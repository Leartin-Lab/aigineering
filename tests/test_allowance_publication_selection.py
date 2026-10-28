from dataclasses import replace
import sqlite3

import pytest

from aigineering.core.allowance_publication_index import (
    PublicationHeadsRequest,
    safe_committed_head_relations,
)
from aigineering.core.causal_allowance import allowance_purpose
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract
from conftest import candidate_runtime


def _identified(contract):
    from aigineering.core.ids import contract_identity_v3

    return replace(contract, id=contract_identity_v3(contract))


def _head_pairs(records):
    pairs = set()
    for record in records:
        if record.record_type != "candidate.committed":
            continue
        relations = safe_committed_head_relations(record)
        if relations is None:
            continue
        members, _parents = relations
        pairs.update((record.id, member_id) for member_id in members)
    return pairs


def _stored_pairs(store):
    return {
        (str(row["head_record_id"]), str(row["member_id"]))
        for row in store._conn.execute(
            "SELECT head_record_id, member_id FROM publication_head_members"
        )
    }


def test_safe_committed_head_relations_preserves_only_indexable_shapes():
    head = create_runtime_record(
        "candidate.committed",
        {"candidate_id": "candidate:one", "committed_record_ids": ["a", "b", "a"]},
        causal_parents=("receipt:one", "a", "b"),
    )

    assert safe_committed_head_relations(head) == (
        frozenset({"a", "b"}),
        frozenset({"receipt:one", "a", "b"}),
    )
    assert (
        safe_committed_head_relations(create_runtime_record("trace.recorded", {}))
        is None
    )
    assert (
        safe_committed_head_relations(
            create_runtime_record(
                "candidate.committed",
                {"committed_record_ids": ["a", 2]},
                causal_parents=("receipt",),
            )
        )
        is None
    )
    assert (
        safe_committed_head_relations(
            create_runtime_record(
                "candidate.committed",
                {"committed_record_ids": "a"},
                causal_parents=("receipt",),
            )
        )
        is None
    )
    malformed_parents = create_runtime_record(
        "candidate.committed",
        {"committed_record_ids": ["a"]},
        causal_parents=("receipt",),
    )
    object.__setattr__(malformed_parents, "causal_parents", ["receipt"])
    assert safe_committed_head_relations(malformed_parents) is None


def test_signed_publications_maintain_exact_head_member_prefix_and_audit():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=8)))
    child = runtime.accept_contract(
        _identified(Contract(id="", parent_id=parent.id, budget=2))
    )

    records = tuple(record for _, record in store.scan_runtime_records())
    assert _stored_pairs(store) == _head_pairs(records)
    state = store._conn.execute(
        "SELECT format_version, through_revision, basis_record_id, has_opaque_heads "
        "FROM publication_index_state WHERE singleton = 1"
    ).fetchone()
    assert state is not None
    assert state["format_version"] == 1
    latest_head_revision, latest_head = max(
        (revision, record)
        for revision, record in store.scan_runtime_records()
        if record.record_type == "candidate.committed"
    )
    assert state["through_revision"] == latest_head_revision
    assert state["basis_record_id"] == latest_head.id
    assert state["has_opaque_heads"] == 0
    assert store.audit_publication_index() == ()
    assert allowance_purpose(child) == "execution"
    store.close()


def test_tail_head_membership_reuses_one_request_set(monkeypatch):
    import aigineering.core.allowance_publication_index as allowance_publication_index
    from aigineering.core.allowance_publication_index import PublicationHeadsRequest

    tail_count = 24
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    runtime.accept_contract(
        _identified(Contract(id="", name="tail-index-prefix", budget=1))
    )
    refresh = store._refresh_publication_index_for_record
    store._refresh_publication_index_for_record = lambda *_args: None
    contracts = tuple(
        runtime.accept_contract(
            _identified(Contract(id="", name=f"tail-source-{index}", budget=1))
        )
        for index in range(tail_count)
    )
    store._refresh_publication_index_for_record = refresh
    records = tuple(record for _, record in store.scan_runtime_records())
    member_ids = tuple(
        record.id
        for record in records
        if record.record_type == "contract.declared"
        and record.payload.get("contract", {}).get("id")
        in {item.id for item in contracts}
    ) + tuple(f"missing:tail:{index}" for index in range(tail_count))
    assert len(member_ids) == tail_count * 2

    seen_arguments = []
    original = allowance_publication_index.safe_committed_head_relations

    class Relations:
        def __init__(self, members, parents):
            self.members = members
            self.parents = parents

        def isdisjoint(self, other):
            seen_arguments.append(other)
            return self.members.isdisjoint(other)

        def intersection(self, other):
            seen_arguments.append(other)
            return self.members.intersection(other)

        def __getitem__(self, index):
            return (self, self.parents)[index]

    def capture_relations(record):
        result = original(record)
        if result is None:
            return None
        return Relations(*result)

    monkeypatch.setattr(
        allowance_publication_index,
        "safe_committed_head_relations",
        capture_relations,
    )
    selection = store.read_allowance_publication_selection(
        PublicationHeadsRequest(member_ids)
    )

    assert selection is not None and selection.complete, (
        store._publication_index_state(),
        seen_arguments,
    )
    assert len(seen_arguments) == tail_count
    assert all(argument is seen_arguments[0] for argument in seen_arguments)
    assert isinstance(seen_arguments[0], frozenset)
    assert len(selection.records) == tail_count
    store.close()


def test_publication_index_audit_is_read_only_and_rebuild_repairs_private_rows():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    runtime.accept_contract(_identified(Contract(id="", budget=8)))
    before_revision = store.get_runtime_revision()
    before_digest = store.runtime_materialization_digest()
    store._conn.execute("DELETE FROM publication_head_members")

    issues = store.audit_publication_index()

    assert issues
    assert store.get_runtime_revision() == before_revision
    assert (
        store._conn.execute("SELECT COUNT(*) FROM publication_head_members").fetchone()[
            0
        ]
        == 0
    )
    assert store.rebuild_publication_index() is None
    assert store.audit_publication_index() == ()
    assert _stored_pairs(store) == _head_pairs(
        tuple(record for _, record in store.scan_runtime_records())
    )
    assert store.runtime_materialization_digest() == before_digest
    store.close()


def test_publication_index_rebuild_reuses_but_never_finishes_outer_transaction():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    runtime.accept_contract(_identified(Contract(id="", budget=8)))
    store._conn.execute("DELETE FROM publication_head_members")
    store._conn.commit()
    store._conn.execute("BEGIN IMMEDIATE")

    store.rebuild_publication_index()

    assert store._conn.in_transaction
    assert store.audit_publication_index() == ()
    store._conn.rollback()
    assert store.audit_publication_index()
    store.close()


def test_publication_index_hook_failure_rolls_back_signed_publication():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=8)))
    before_revision = store.get_runtime_revision()
    before_digest = store.runtime_materialization_digest()
    before_pairs = _stored_pairs(store)
    before_state = tuple(store._publication_index_state())
    store._conn.execute(
        "CREATE TRIGGER reject_publication_index_member "
        "BEFORE INSERT ON publication_head_members "
        "BEGIN SELECT RAISE(ABORT, 'forced publication index write failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced publication index"):
        runtime.accept_contract(
            _identified(Contract(id="", parent_id=parent.id, budget=2))
        )

    assert store.get_runtime_revision() == before_revision
    assert store.runtime_materialization_digest() == before_digest
    assert _stored_pairs(store) == before_pairs
    assert tuple(store._publication_index_state()) == before_state
    assert store.audit_publication_index() == ()
    store.close()


def test_outer_publication_index_rebuild_failure_leaves_rollback_to_caller():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    runtime.accept_contract(_identified(Contract(id="", budget=8)))
    before_pairs = _stored_pairs(store)
    before_state = tuple(store._publication_index_state())
    store._conn.execute(
        "CREATE TRIGGER reject_publication_index_member "
        "BEFORE INSERT ON publication_head_members "
        "BEGIN SELECT RAISE(ABORT, 'forced publication index write failure'); END"
    )
    store._conn.commit()
    store._conn.execute("BEGIN IMMEDIATE")
    store._conn.execute("DELETE FROM publication_head_members")

    with pytest.raises(sqlite3.IntegrityError, match="forced publication index"):
        store.rebuild_publication_index()

    assert store._conn.in_transaction
    assert _stored_pairs(store) == set()
    store._conn.rollback()
    assert _stored_pairs(store) == before_pairs
    assert tuple(store._publication_index_state()) == before_state
    assert store.audit_publication_index() == ()
    store.close()


def test_missing_publication_table_uses_reference_without_recreating_it():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=8)))
    store._conn.execute("DROP TABLE publication_head_members")
    store._conn.commit()

    assert (
        store.read_allowance_publication_selection(
            PublicationHeadsRequest(("record:missing",))
        )
        is None
    )
    child = runtime.accept_contract(
        _identified(Contract(id="", parent_id=parent.id, budget=2))
    )
    table_names = {
        str(row[0])
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "publication_head_members" not in table_names
    assert store.get_contract(child.id) == child
    store.close()


def test_reference_mode_does_not_maintain_publication_relations():
    store = SQLiteStore(":memory:", allowance_checkpoint_mode="reference")
    runtime = candidate_runtime(store)
    runtime.accept_contract(_identified(Contract(id="", budget=8)))

    assert (
        store._conn.execute("SELECT COUNT(*) FROM publication_head_members").fetchone()[
            0
        ]
        == 0
    )
    assert (
        store._conn.execute("SELECT COUNT(*) FROM publication_index_state").fetchone()[
            0
        ]
        == 0
    )
    store.close()


def test_non_head_records_do_not_advance_publication_index_state(tmp_path):
    store = SQLiteStore(str(tmp_path / "non-head-index-cursor.sqlite"))
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=8)))
    before = store._publication_index_state()
    assert before is not None
    refreshes = []
    refresh_index = store._refresh_publication_index_for_record

    def capture_refresh(record, revision):
        refreshes.append((record.record_type, revision))
        return refresh_index(record, revision)

    store._refresh_publication_index_for_record = capture_refresh

    extra = create_runtime_record("trace.recorded", {"entry_id": "trace:extra"})
    store._conn.execute("BEGIN IMMEDIATE")
    store._insert_runtime_record(extra)
    store._conn.execute("COMMIT")

    after_non_head = store._publication_index_state()
    assert refreshes == []
    assert after_non_head["through_revision"] == before["through_revision"]
    assert after_non_head["basis_record_id"] == before["basis_record_id"]

    child = _identified(Contract(id="", parent_id=parent.id, budget=2))
    runtime.accept_contract(child)
    assert len(refreshes) == 1
    assert refreshes[0][0] == "candidate.committed"
    after_head = store._publication_index_state()
    committed_head_revisions = tuple(
        revision
        for revision, record in store.scan_runtime_records()
        if record.record_type == "candidate.committed"
    )
    assert after_head["through_revision"] == max(committed_head_revisions)
    assert after_head["basis_record_id"] == next(
        record.id
        for revision, record in store.scan_runtime_records()
        if revision == after_head["through_revision"]
    )
    assert store.audit_publication_index() == ()
    store.close()


def test_publication_selection_chunks_against_connection_variable_limit(tmp_path):
    from aigineering.core.allowance_publication_index import (
        DeclarationPairsRequest,
        PublicationClosureRequest,
        PublicationHeadsRequest,
    )

    store = SQLiteStore(str(tmp_path / "publication-query-limit.sqlite"))
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=8)))
    records = tuple(record for _, record in store.scan_runtime_records())
    head = next(
        record for record in records if record.record_type == "candidate.committed"
    )
    declaration = next(
        record for record in records if record.record_type == "contract.declared"
    )
    members, _parents = safe_committed_head_relations(head)
    pair = (declaration.payload["candidate_id"], contract.id)
    store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 199)

    head_selection = store.read_allowance_publication_selection(
        PublicationHeadsRequest(
            tuple(f"member:{index}" for index in range(199)) + (next(iter(members)),)
        )
    )
    closure_selection = store.read_allowance_publication_selection(
        PublicationClosureRequest(
            tuple(f"record:{index}" for index in range(199)) + (head.id,)
        )
    )
    declaration_selection = store.read_allowance_publication_selection(
        DeclarationPairsRequest(
            tuple((f"candidate:{index}", f"contract:{index}") for index in range(99))
            + (pair,)
        )
    )

    assert head_selection is not None and head_selection.complete
    assert head.id in {record.id for record in head_selection.records}
    assert closure_selection is not None and closure_selection.complete
    assert head.id in {record.id for record in closure_selection.records}
    assert declaration_selection is not None and declaration_selection.complete
    assert declaration.id in {record.id for record in declaration_selection.records}
    store.close()


@pytest.mark.parametrize(
    ("request_factory", "variable_limit"),
    [
        ("heads", 1),
        ("closure", 1),
        ("declarations", 1),
    ],
)
def test_publication_selection_reports_unusable_variable_budget(
    tmp_path, request_factory, variable_limit
):
    from aigineering.core.allowance_publication_index import (
        DeclarationPairsRequest,
        PublicationClosureRequest,
        PublicationHeadsRequest,
    )

    store = SQLiteStore(str(tmp_path / f"publication-limit-{request_factory}.sqlite"))
    runtime = candidate_runtime(store)
    runtime.accept_contract(_identified(Contract(id="", budget=8)))
    store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, variable_limit)
    requests = {
        "heads": PublicationHeadsRequest(("member:one",)),
        "closure": PublicationClosureRequest(("record:one",)),
        "declarations": DeclarationPairsRequest((("candidate:one", "contract:one"),)),
    }

    with pytest.raises(sqlite3.OperationalError, match="variable limit"):
        store.read_allowance_publication_selection(requests[request_factory])
    store.close()
