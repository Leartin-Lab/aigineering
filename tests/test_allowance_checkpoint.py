import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import aigineering.core.sqlite_store as sqlite_store_module
from aigineering.core.sqlite_store import CURRENT_SCHEMA_VERSION, SQLiteStore
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.candidate_publisher import (
    CandidatePublisherRegistry,
    publish_effect,
)
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.plugins.handoff import HANDOFF_EFFECTS
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    task_handoff_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.types import Contract
from aigineering.protocol.identity import contract_identity_v3
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.agent.worker import WorkerExecutionError
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    process_expired_claims,
)
from conftest import candidate_runtime, hosted_worker


def _checkpoint_row(store, contract_id):
    return store._conn.execute(
        "SELECT * FROM allowance_checkpoints WHERE contract_id = ?", (contract_id,)
    ).fetchone()


def _identified(contract):
    return Contract(**{**contract.__dict__, "id": contract_identity_v3(contract)})


def test_signed_publication_materializes_checkpoint_without_schema_floor_change():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=40)))

    row = _checkpoint_row(store, parent.id)

    assert row is not None
    assert row["status"] == "valid"
    assert row["grant_count"] == "1"
    assert row["granted_total"] == "40"
    assert int(row["through_revision"]) <= store.get_runtime_revision()
    assert (
        store._conn.execute(
            "SELECT 1 FROM runtime_records WHERE revision = ?",
            (row["through_revision"],),
        ).fetchone()
        is not None
    )
    assert CURRENT_SCHEMA_VERSION == 21
    store.close()


def test_reference_mode_keeps_facts_and_skips_checkpoint_writes(tmp_path: Path):
    store = SQLiteStore(
        str(tmp_path / "reference.sqlite"), allowance_checkpoint_mode="reference"
    )
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=12)))

    assert _checkpoint_row(store, contract.id) is None
    assert store.scan_runtime_records(record_type="allowance.granted")
    store.close()


def test_checkpoint_read_refreshes_tail_without_writing_it(tmp_path: Path):
    path = str(tmp_path / "tail.sqlite")
    store = SQLiteStore(path)
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=40)))
    before = _checkpoint_row(store, parent.id)
    reference = SQLiteStore(path, allowance_checkpoint_mode="reference")
    old_writer_runtime = candidate_runtime(
        reference,
        genesis=runtime.genesis,
        actor_key=runtime.actor_key,
        signer=runtime.signer,
    )
    old_writer_runtime._sequence = runtime._sequence
    child = _identified(Contract(id="", parent_id=parent.id, budget=7))
    child = old_writer_runtime.accept_contract(child)

    snapshot = store.read_raw_allowance_checkpoint(parent.id)
    after = _checkpoint_row(store, parent.id)

    assert snapshot.status == "valid"
    assert snapshot.totals.grant_count == 1
    assert snapshot.totals.granted_total == 40
    assert snapshot.totals.reserved_total == 7
    assert snapshot.tail_record_count == 1
    assert before["through_revision"] == after["through_revision"]
    assert reference.get_contract(child.id) is not None
    store.close()
    reference.close()


def test_checkpoint_tail_and_query_ranges_are_independent_of_history(
    tmp_path: Path, monkeypatch
):
    callbacks_by_history = {}
    for historical_children in (0, 16, 64):
        path = str(tmp_path / f"range-{historical_children}.sqlite")
        store = SQLiteStore(path)
        runtime = candidate_runtime(store)
        parent = runtime.accept_contract(_identified(Contract(id="", budget=500)))
        for index in range(historical_children):
            runtime.accept_contract(
                _identified(
                    Contract(
                        id="", name=f"historical-{index}", parent_id=parent.id, budget=1
                    )
                )
            )
        reference = SQLiteStore(path, allowance_checkpoint_mode="reference")
        old_writer = candidate_runtime(
            reference,
            genesis=runtime.genesis,
            actor_key=runtime.actor_key,
            signer=runtime.signer,
        )
        old_writer._sequence = runtime._sequence
        old_writer.accept_contract(
            _identified(Contract(id="", name="tail", parent_id=parent.id, budget=1))
        )

        cursor_before = int(_checkpoint_row(store, parent.id)["through_revision"])
        through = store.get_runtime_revision()
        returned_revisions = set()
        for record_type, field in store._ALLOWANCE_RELATIONS:
            sql = (
                "SELECT * FROM runtime_records WHERE record_type = ? "
                f"AND json_extract(payload_json, '$.{field}') = ? "
                "AND revision > ? AND revision <= ? ORDER BY revision"
            )
            returned_revisions.update(
                int(row["revision"])
                for row in store._conn.execute(
                    sql, (record_type, parent.id, cursor_before, through)
                ).fetchall()
            )
        actual_decoded = []
        original_decode = store._row_to_runtime_record

        def count_decode(row):
            actual_decoded.append(row["record_id"])
            return original_decode(row)

        actual_fold_sizes = []
        original_fold = sqlite_store_module.fold_raw_allowance_records

        def count_fold(contract_id, records):
            materialized = tuple(records)
            actual_fold_sizes.append(len(materialized))
            return original_fold(contract_id, materialized)

        monkeypatch.setattr(store, "_row_to_runtime_record", count_decode)
        monkeypatch.setattr(
            sqlite_store_module, "fold_raw_allowance_records", count_fold
        )
        callbacks = [0]

        def count_vm_steps():
            callbacks[0] += 1
            return 0

        before = _checkpoint_row(store, parent.id)["through_revision"]
        store._conn.set_progress_handler(count_vm_steps, 1)
        snapshot = store.read_raw_allowance_checkpoint(parent.id)
        store._conn.set_progress_handler(None, 0)
        after = _checkpoint_row(store, parent.id)["through_revision"]
        callbacks_by_history[historical_children] = callbacks[0]

        expected_rows = historical_children + 1
        assert snapshot.status == "valid"
        assert snapshot.tail_record_count == 1
        assert snapshot.decoded_record_count == 1
        assert snapshot.fold_record_count == 1
        assert len(returned_revisions) == 1
        assert len(actual_decoded) == len(returned_revisions)
        assert actual_fold_sizes == [len(returned_revisions)]
        assert snapshot.totals.reserved_total == expected_rows
        assert before == after  # Read path did not warm or rewrite the cache.
        for record_type, field in store._ALLOWANCE_RELATIONS:
            sql = (
                "SELECT * FROM runtime_records WHERE record_type = ? "
                f"AND json_extract(payload_json, '$.{field}') = ? "
                "AND revision > ? AND revision <= ? ORDER BY revision"
            )
            detail = (
                store._conn.execute(
                    "EXPLAIN QUERY PLAN " + sql,
                    (record_type, parent.id, 0, store.get_runtime_revision()),
                )
                .fetchone()["detail"]
                .lower()
            )
            assert "search runtime_records" in detail
            assert "rowid>?" in detail and "rowid<?" in detail
        reference.close()
        store.close()
        monkeypatch.undo()

    baseline = callbacks_by_history[0]
    assert callbacks_by_history[16] <= 2 * baseline + 64
    assert callbacks_by_history[64] <= 2 * baseline + 64


def test_cache_only_rebuild_repairs_derived_row_and_audit_is_read_only():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=19)))
    facts_before = tuple(record.id for _, record in store.scan_runtime_records())
    digest_before = store.runtime_materialization_digest()
    store._conn.execute(
        "UPDATE allowance_checkpoints SET granted_total='999' WHERE contract_id=?",
        (contract.id,),
    )

    assert store.audit_allowance_checkpoints()
    with pytest.raises(RuntimeError, match="allowance checkpoint integrity"):
        store.runtime_materialization_digest()
    store.rebuild_allowance_checkpoints()

    assert store.audit_allowance_checkpoints() == ()
    assert store.runtime_materialization_digest() == digest_before
    store._conn.execute(
        "DELETE FROM allowance_checkpoints WHERE contract_id=?", (contract.id,)
    )
    assert store.runtime_materialization_digest() == digest_before
    assert store.read_raw_allowance_checkpoint(contract.id).totals.granted_total == 19
    assert (
        tuple(record.id for _, record in store.scan_runtime_records()) == facts_before
    )
    store.close()


def test_checkpoint_rebuild_reuses_but_never_finishes_outer_transaction():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=19)))
    store._conn.execute("BEGIN IMMEDIATE")
    store._conn.execute(
        "UPDATE allowance_checkpoints SET granted_total='888' WHERE contract_id=?",
        (contract.id,),
    )

    store.rebuild_allowance_checkpoints()

    assert store._conn.in_transaction
    store._conn.execute("ROLLBACK")
    assert store.audit_allowance_checkpoints() == ()
    store.rebuild_allowance_checkpoints()
    assert store.audit_allowance_checkpoints() == ()
    store.close()


def test_cache_only_rebuild_failure_preserves_outer_transaction(monkeypatch):
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=19)))
    before = _checkpoint_row(store, contract.id)["granted_total"]
    store._conn.execute("BEGIN IMMEDIATE")
    store._conn.execute(
        "UPDATE allowance_checkpoints SET granted_total='888' WHERE contract_id=?",
        (contract.id,),
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected cache-only rebuild failure")

    monkeypatch.setattr(store, "_write_allowance_checkpoint", fail)
    with pytest.raises(RuntimeError, match="cache-only rebuild failure"):
        store.rebuild_allowance_checkpoints()
    assert store._conn.in_transaction
    # The caller owns its outer transaction and must decide whether to roll it
    # back; the failed rebuild neither commits nor rolls back the DELETE.
    assert _checkpoint_row(store, contract.id) is None
    store._conn.execute("ROLLBACK")
    assert _checkpoint_row(store, contract.id)["granted_total"] == before
    assert store.audit_allowance_checkpoints() == ()
    store.close()


def test_cache_only_rebuild_failure_rolls_back_only_its_own_transaction(monkeypatch):
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=19)))
    store._conn.execute(
        "UPDATE allowance_checkpoints SET granted_total='888' WHERE contract_id=?",
        (contract.id,),
    )
    store._conn.commit()
    corrupted = _checkpoint_row(store, contract.id)["granted_total"]

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected owned rebuild failure")

    monkeypatch.setattr(store, "_write_allowance_checkpoint", fail)
    with pytest.raises(RuntimeError, match="owned rebuild failure"):
        store.rebuild_allowance_checkpoints()
    assert not store._conn.in_transaction
    assert _checkpoint_row(store, contract.id)["granted_total"] == corrupted
    assert store.audit_allowance_checkpoints()
    store.close()


def test_cache_only_rebuild_serializes_two_connections(tmp_path: Path):
    path = str(tmp_path / "rebuild-lock.sqlite")
    setup = SQLiteStore(path)
    runtime = candidate_runtime(setup)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=19)))
    setup.close()

    owner = SQLiteStore(path)
    contender = SQLiteStore(path)
    contender._conn.execute("PRAGMA busy_timeout=0")
    owner._conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            contender.rebuild_allowance_checkpoints()
        assert not contender._conn.in_transaction
    finally:
        owner._conn.execute("ROLLBACK")
    contender.rebuild_allowance_checkpoints()
    assert (
        contender.read_raw_allowance_checkpoint(contract.id).totals.granted_total == 19
    )
    assert contender.audit_allowance_checkpoints() == ()
    owner.close()
    contender.close()


def test_missing_private_table_is_a_read_only_cache_miss():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=19)))
    store._conn.execute("DROP TABLE allowance_checkpoints")

    snapshot = store.read_raw_allowance_checkpoint(contract.id)

    assert snapshot.status == "valid"
    assert snapshot.totals.granted_total == 19
    assert (
        store._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='allowance_checkpoints'"
        ).fetchone()
        is None
    )
    store.close()


def test_missing_table_writer_hook_and_snapshot_from_path_are_nonmutating(
    tmp_path: Path,
):
    path = tmp_path / "missing-checkpoint.sqlite"
    store = SQLiteStore(str(path))
    store._conn.execute("DROP TABLE allowance_checkpoints")
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=19)))
    assert store.get_contract(contract.id) == contract
    assert (
        store._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='allowance_checkpoints'"
        ).fetchone()
        is None
    )
    store.close()

    before = sqlite3.connect(path)
    try:
        tables_before = tuple(
            row[0]
            for row in before.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
    finally:
        before.close()
    snapshot = SQLiteStore.snapshot_from_path(path)
    try:
        assert snapshot.read_raw_allowance_checkpoint(contract.id).status == "valid"
        assert (
            snapshot.read_raw_allowance_checkpoint(contract.id).totals.granted_total
            == 19
        )
        assert (
            snapshot._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='allowance_checkpoints'"
            ).fetchone()
            is None
        )
    finally:
        snapshot.close()
    after = sqlite3.connect(path)
    try:
        tables_after = tuple(
            row[0]
            for row in after.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
    finally:
        after.close()
    assert tables_after == tables_before


def test_snapshot_reopen_and_strict_rebuild_keep_checkpoint_semantics(tmp_path: Path):
    path = str(tmp_path / "snapshot-reopen.sqlite")
    store = SQLiteStore(path)
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=30)))
    runtime.accept_contract(_identified(Contract(id="", parent_id=parent.id, budget=4)))
    digest = store.runtime_materialization_digest()
    facts = tuple(record.id for _, record in store.scan_runtime_records())

    snapshot = store.snapshot()
    try:
        assert (
            snapshot.read_raw_allowance_checkpoint(parent.id).totals.reserved_total == 4
        )
        assert snapshot.audit_allowance_checkpoints() == ()
        assert (
            tuple(record.id for _, record in snapshot.scan_runtime_records()) == facts
        )
    finally:
        snapshot.close()
    store.close()

    reopened = SQLiteStore(path)
    try:
        assert reopened.runtime_materialization_digest() == digest
        assert (
            reopened.read_raw_allowance_checkpoint(parent.id).totals.reserved_total == 4
        )
        assert reopened.rebuild_runtime_materializations(strict=True) == digest
        assert reopened.audit_allowance_checkpoints() == ()
        assert (
            tuple(record.id for _, record in reopened.scan_runtime_records()) == facts
        )
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("format_version", 99),
        ("through_revision", 10**9),
        ("basis_record_id", "wrong-basis"),
        ("granted_total", "0019"),
        ("granted_total", "19.0"),
    ),
)
def test_invalid_checkpoint_metadata_is_opaque_and_not_repaired(field, value):
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=19)))
    before = _checkpoint_row(store, contract.id)[field]
    store._conn.execute(
        f"UPDATE allowance_checkpoints SET {field} = ? WHERE contract_id = ?",
        (value, contract.id),
    )
    invalid_value = _checkpoint_row(store, contract.id)[field]

    snapshot = store.read_raw_allowance_checkpoint(contract.id)

    assert snapshot.status == "opaque"
    assert _checkpoint_row(store, contract.id)[field] == invalid_value
    assert before != invalid_value
    store.close()


def test_checkpoint_schema_rejects_unknown_status():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=19)))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        store._conn.execute(
            "UPDATE allowance_checkpoints SET status='unknown' WHERE contract_id=?",
            (contract.id,),
        )
    store.close()


def test_checkpoint_dml_failure_rolls_back_new_signed_fact(monkeypatch):
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    candidate = _identified(Contract(id="", budget=19))

    def fail_write(_contract_id, _through, _totals):
        raise sqlite3.OperationalError("checkpoint write failed")

    monkeypatch.setattr(store, "_write_allowance_checkpoint", fail_write)
    with pytest.raises(sqlite3.OperationalError, match="checkpoint write failed"):
        runtime.accept_contract(candidate)

    assert store.get_contract(candidate.id) is None
    assert not store.scan_runtime_records(record_type="allowance.granted")
    store.close()


def test_unconvertible_historical_amount_moves_valid_checkpoint_to_opaque():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=19)))
    record = create_runtime_record(
        "allowance.reserved",
        {
            "amount": "historically-invalid",
            "child_contract_id": "legacy-child",
            "purpose": "execution",
            "reservation_id": "legacy-reservation",
            "source_contract_id": parent.id,
        },
    )

    # Simulate a legacy immutable row accepted before the checkpoint existed.
    with store._conn:
        store._insert_runtime_record(record)

    row = _checkpoint_row(store, parent.id)
    assert row["status"] == "opaque"
    assert row["granted_total"] is None
    assert store.read_raw_allowance_checkpoint(parent.id).status == "opaque"
    opaque_through = row["through_revision"]
    good_later = create_runtime_record(
        "allowance.reserved",
        {
            "amount": 1,
            "child_contract_id": "later-good-child",
            "purpose": "execution",
            "reservation_id": "later-good-reservation",
            "source_contract_id": parent.id,
        },
        recorded_at="2026-01-01T00:00:01Z",
    )
    with store._conn:
        store._insert_runtime_record(good_later)
    preserved = _checkpoint_row(store, parent.id)
    assert preserved["status"] == "opaque"
    assert preserved["through_revision"] == opaque_through
    assert store.read_raw_allowance_checkpoint(parent.id).status == "opaque"
    store.close()


def test_arbitrary_precision_raw_totals_survive_checkpoint_and_rebuild():
    """Raw pressure only: no multi-grant authorization is asserted here."""
    store = SQLiteStore(":memory:")
    account = "raw-big-integer"
    first_amount = "9" * 4300
    second_amount = "8" * 4300
    first = create_runtime_record(
        "allowance.granted",
        {
            "amount": first_amount,
            "contract_id": account,
            "grant_id": "raw-first",
            "purpose": "root",
            "source_contract_id": "",
        },
    )
    second = create_runtime_record(
        "allowance.granted",
        {
            "amount": second_amount,
            "contract_id": account,
            "grant_id": "raw-second",
            "purpose": "root",
            "source_contract_id": "",
        },
    )
    with store._conn:
        store._insert_runtime_record(first)
        store._insert_runtime_record(second)

    expected = int(first_amount) + int(second_amount)
    snapshot = store.read_raw_allowance_checkpoint(account)
    assert snapshot.status == "valid"
    assert snapshot.totals is not None
    assert snapshot.totals.granted_total == expected
    assert store.audit_allowance_checkpoints() == ()
    store.rebuild_allowance_checkpoints()
    rebuilt = store.read_raw_allowance_checkpoint(account)
    assert rebuilt.status == "valid"
    assert rebuilt.totals is not None
    assert rebuilt.totals.granted_total == expected
    assert store.audit_allowance_checkpoints() == ()
    store.close()


def test_returned_self_relation_queries_deduplicate_before_decode(monkeypatch):
    store = SQLiteStore(":memory:")
    account = "raw-query-self-return"
    record = create_runtime_record(
        "allowance.returned",
        {
            "source_contract_id": account,
            "from_contract_id": account,
            "amount": 4,
        },
    )
    with store._conn:
        store._insert_runtime_record(record)
        store._conn.execute(
            "DELETE FROM allowance_checkpoints WHERE contract_id = ?", (account,)
        )

    raw_return_rows = {}
    for field in ("source_contract_id", "from_contract_id"):
        raw_return_rows[field] = len(
            store._conn.execute(
                "SELECT * FROM runtime_records WHERE record_type = ? "
                f"AND json_extract(payload_json, '$.{field}') = ? "
                "AND revision > ? AND revision <= ? ORDER BY revision",
                ("allowance.returned", account, 0, store.get_runtime_revision()),
            ).fetchall()
        )

    sql_counts = {"source_contract_id": 0, "from_contract_id": 0}

    def trace(statement: str) -> None:
        for field in sql_counts:
            if f"json_extract(payload_json, '$.{field}')" in statement:
                sql_counts[field] += 1

    store._conn.set_trace_callback(trace)
    original_decode = store._row_to_runtime_record
    decoded = []

    def decode(row):
        decoded.append(row["record_id"])
        return original_decode(row)

    monkeypatch.setattr(store, "_row_to_runtime_record", decode)
    try:
        checkpoint = store.read_raw_allowance_checkpoint(account)
    finally:
        store._conn.set_trace_callback(None)

    # source_contract_id is queried for both reserved and returned facts.
    assert sql_counts == {"source_contract_id": 2, "from_contract_id": 1}
    assert raw_return_rows == {"source_contract_id": 1, "from_contract_id": 1}
    assert decoded == [record.id]
    assert checkpoint.status == "valid"
    assert checkpoint.tail_record_count == 1
    assert checkpoint.decoded_record_count == 1
    assert checkpoint.fold_record_count == 1
    assert checkpoint.totals.returned_in_total == 4
    assert checkpoint.totals.returned_out_total == 4
    store.close()


def test_reference_read_binds_totals_and_revision_to_one_snapshot(
    tmp_path: Path, monkeypatch
):
    path = str(tmp_path / "reference-snapshot.sqlite")
    setup = SQLiteStore(path)
    setup_runtime = candidate_runtime(setup)
    parent = setup_runtime.accept_contract(_identified(Contract(id="", budget=30)))
    genesis = setup_runtime.genesis
    actor_key = setup_runtime.actor_key
    signer = setup_runtime.signer
    setup.close()

    reader = SQLiteStore(path, allowance_checkpoint_mode="reference")
    writer = SQLiteStore(path, allowance_checkpoint_mode="reference")
    writer_runtime = candidate_runtime(
        writer, genesis=genesis, actor_key=actor_key, signer=signer
    )
    original_scan = reader.scan_runtime_records
    interleaved = False

    def scan_then_commit(**kwargs):
        nonlocal interleaved
        captured = original_scan(**kwargs)
        if not interleaved:
            interleaved = True
            writer_runtime._sequence = 500
            writer_runtime.accept_contract(
                _identified(
                    Contract(id="", name="concurrent", parent_id=parent.id, budget=1)
                )
            )
        return captured

    monkeypatch.setattr(reader, "scan_runtime_records", scan_then_commit)
    captured = reader.read_raw_allowance_checkpoint(parent.id)

    assert captured.totals.reserved_total == 0
    assert captured.through_revision < writer.get_runtime_revision()
    assert reader.read_raw_allowance_checkpoint(parent.id).totals.reserved_total == 1
    reader.close()
    writer.close()


def test_two_sqlite_writers_keep_checkpoint_equal_to_accepted_facts(tmp_path: Path):
    path = str(tmp_path / "race.sqlite")
    setup = SQLiteStore(path)
    runtime = candidate_runtime(setup)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=3)))
    setup.close()
    children = tuple(
        _identified(
            Contract(id="", name=f"racer-{index}", parent_id=parent.id, budget=2)
        )
        for index in range(2)
    )

    def publish(index: int) -> bool:
        store = SQLiteStore(path)
        try:
            worker = candidate_runtime(
                store,
                genesis=runtime.genesis,
                actor_key=runtime.actor_key,
                signer=runtime.signer,
            )
            worker._sequence = 100 + index
            try:
                worker.accept_contract(children[index])
            except AssertionError:
                return False
            return True
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(publish, range(2)))

    store = SQLiteStore(path)
    try:
        assert sum(results) == 1
        reservations = store.scan_runtime_records(record_type="allowance.reserved")
        assert sum(int(record.payload["amount"]) for _, record in reservations) == 2
        snapshot = store.read_raw_allowance_checkpoint(parent.id)
        assert snapshot.status == "valid"
        assert snapshot.totals.reserved_total == 2
        assert store.audit_allowance_checkpoints() == ()
    finally:
        store.close()


def test_signed_handoff_return_updates_both_checkpoint_accounts_and_replay():
    store = SQLiteStore(":memory:")
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:checkpoint-handoff",
        "checkpoint-handoff-key",
        signer.kind,
        signer.signer_id,
        ("contract.cancel", "contract.publish", "task.handoff", "task.block"),
    )
    genesis = create_genesis_manifest(
        "checkpoint-handoff", (actor,), "policy:checkpoint-handoff"
    )
    initialize_genesis(store, genesis)

    def contract(name, *, parent_id=None, budget=5):
        return contract_from_fields(
            name=name,
            description=name,
            parent_id=parent_id,
            inputs=(),
            outputs=(f"{name}.out",),
            activation="",
            budget=budget,
            tool_scope=(),
            labels=(),
            origin="human",
        )

    source = contract("checkpoint-handoff-source")
    source_result = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(source),
        idempotency_key="checkpoint-handoff-source",
    )
    assert source_result.accepted
    source = source_result.contract
    child = contract("checkpoint-handoff-child", parent_id=source.id, budget=5)
    child_result = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(child),
        idempotency_key="checkpoint-handoff-child",
    )
    assert child_result.accepted
    child = child_result.contract

    def handoff():
        return publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            task_handoff_effect(source.id, "Continue the root obligation"),
            idempotency_key="checkpoint-handoff-return",
            additional_effects=HANDOFF_EFFECTS,
        )

    accepted = handoff()
    assert accepted.accepted
    returned = tuple(
        record
        for _, record in store.scan_runtime_records(record_type="allowance.returned")
    )
    assert len(returned) == 1
    source_snapshot = store.read_raw_allowance_checkpoint(source.id)
    child_snapshot = store.read_raw_allowance_checkpoint(child.id)
    records = tuple(record for _, record in store.scan_runtime_records())
    assert source_snapshot.totals.returned_in_total == 5
    assert child_snapshot.totals.returned_out_total == 5
    assert source_snapshot.totals.returned_in_total == sum(
        int(record.payload["amount"])
        for record in returned
        if record.payload["source_contract_id"] == source.id
    )
    assert child_snapshot.totals.returned_out_total == sum(
        int(record.payload["amount"])
        for record in returned
        if record.payload["from_contract_id"] == child.id
    )
    assert (
        allowance_available(source, records)
        == source_snapshot.totals.granted_total
        - source_snapshot.totals.reserved_total
        + source_snapshot.totals.returned_in_total
        - source_snapshot.totals.returned_out_total
        - source_snapshot.totals.extinguished_total
    )
    assert (
        allowance_available(child, records)
        == child_snapshot.totals.granted_total
        - child_snapshot.totals.reserved_total
        + child_snapshot.totals.returned_in_total
        - child_snapshot.totals.returned_out_total
        - child_snapshot.totals.extinguished_total
    )

    replay = handoff()
    assert replay.accepted
    assert (
        tuple(
            record
            for _, record in store.scan_runtime_records(
                record_type="allowance.returned"
            )
        )
        == returned
    )
    assert (
        store.read_raw_allowance_checkpoint(source.id).totals == source_snapshot.totals
    )
    assert store.read_raw_allowance_checkpoint(child.id).totals == child_snapshot.totals
    assert store.audit_allowance_checkpoints() == ()
    store.close()


def test_paired_return_checkpoint_second_account_failure_rolls_back_both(
    monkeypatch,
):
    store = SQLiteStore(":memory:")
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:checkpoint-return-rollback",
        "checkpoint-return-rollback-key",
        signer.kind,
        signer.signer_id,
        ("contract.cancel", "contract.publish", "task.handoff", "task.block"),
    )
    genesis = create_genesis_manifest(
        "checkpoint-return-rollback", (actor,), "policy:checkpoint-return-rollback"
    )
    initialize_genesis(store, genesis)

    def contract(name, *, parent_id=None, budget=5):
        return contract_from_fields(
            name=name,
            description=name,
            parent_id=parent_id,
            inputs=(),
            outputs=(f"{name}.out",),
            activation="",
            budget=budget,
            tool_scope=(),
            labels=(),
            origin="human",
        )

    source_result = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(contract("checkpoint-rollback-source")),
        idempotency_key="checkpoint-rollback-source",
    )
    assert source_result.accepted
    source = source_result.contract
    child_result = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(
            contract("checkpoint-rollback-child", parent_id=source.id)
        ),
        idempotency_key="checkpoint-rollback-child",
    )
    assert child_result.accepted
    child = child_result.contract
    before = {
        source.id: store.read_raw_allowance_checkpoint(source.id).totals,
        child.id: store.read_raw_allowance_checkpoint(child.id).totals,
    }
    original_refresh = store._refresh_allowance_checkpoint_for_record
    original_write = store._write_allowance_checkpoint
    touched = []

    def wrapped_refresh(record, revision):
        if record.record_type != "allowance.returned":
            return original_refresh(record, revision)

        def fail_second_account(account_id, through, totals):
            if account_id in {source.id, child.id}:
                touched.append(account_id)
                if len(touched) == 2:
                    raise RuntimeError(
                        "injected paired checkpoint second-account failure"
                    )
            return original_write(account_id, through, totals)

        monkeypatch.setattr(store, "_write_allowance_checkpoint", fail_second_account)
        try:
            return original_refresh(record, revision)
        finally:
            monkeypatch.setattr(store, "_write_allowance_checkpoint", original_write)

    monkeypatch.setattr(
        store, "_refresh_allowance_checkpoint_for_record", wrapped_refresh
    )
    with pytest.raises(RuntimeError, match="second-account failure"):
        publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            task_handoff_effect(source.id, "Continue after return failure"),
            idempotency_key="checkpoint-return-rollback-handoff",
            additional_effects=HANDOFF_EFFECTS,
        )

    assert set(touched) == {source.id, child.id}
    assert not store.scan_runtime_records(record_type="allowance.returned")
    assert not store.scan_runtime_records(record_type="task.handed_off")
    assert store.read_raw_allowance_checkpoint(source.id).totals == before[source.id]
    assert store.read_raw_allowance_checkpoint(child.id).totals == before[child.id]
    assert store.audit_allowance_checkpoints() == ()
    store.close()


def test_signed_worker_failure_writer_updates_terminal_checkpoint(monkeypatch):
    store = SQLiteStore(":memory:")
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(id="task:checkpoint-failure", outputs=("report",), budget=3)
    )

    class FailingWorker:
        worker_id = "worker:checkpoint-failure"

        def invoke(self, _contract, _disclosed_assets):
            raise WorkerExecutionError("fixture_timeout", "safe timeout")

    host = hosted_worker(
        store,
        FailingWorker(),
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    claim_candidate = host.claim_candidate(
        request_id="checkpoint-failure-claim",
        contract_id=contract.id,
        lease_seconds=60,
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract.id,
        lease_seconds=60,
        claim_candidate=claim_candidate,
    )
    assert claimed is not None
    original_write = store._write_allowance_checkpoint

    def fail_checkpoint(contract_id, through, totals):
        if contract_id == contract.id:
            raise RuntimeError("injected worker-failure checkpoint write failure")
        return original_write(contract_id, through, totals)

    monkeypatch.setattr(store, "_write_allowance_checkpoint", fail_checkpoint)
    with pytest.raises(RuntimeError, match="worker-failure checkpoint write failure"):
        execute_claimed_package(claimed, host, store)
    assert store.get_claim(contract.id)["status"] == "active"
    assert not store.scan_runtime_records(record_type="worker.invocation_failed")
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    assert not store.scan_runtime_records(record_type="allowance.extinguished")
    assert (
        store.read_raw_allowance_checkpoint(contract.id).totals.extinguished_total == 0
    )
    monkeypatch.setattr(store, "_write_allowance_checkpoint", original_write)

    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(claimed, host, store)

    snapshot = store.read_raw_allowance_checkpoint(contract.id)
    terminal = store.scan_runtime_records(record_type="lifecycle.terminal")[-1][1]
    extinguishment = store.scan_runtime_records(record_type="allowance.extinguished")[
        -1
    ][1]
    assert terminal.payload["contract_id"] == contract.id
    assert extinguishment.payload["contract_id"] == contract.id
    assert snapshot.status == "valid"
    assert snapshot.totals.extinguished_total == 3
    assert snapshot.totals.granted_total == 3
    assert store.audit_allowance_checkpoints() == ()
    store.close()


def test_signed_claim_expiration_writer_updates_terminal_checkpoint(monkeypatch):
    store = SQLiteStore(":memory:")
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(id="task:checkpoint-expiration", outputs=("report",), budget=5)
    )

    class IdleWorker:
        worker_id = "worker:checkpoint-expiration"

        def invoke(self, _contract, _disclosed_assets):
            raise AssertionError("expired work must not execute")

    host = hosted_worker(
        store,
        IdleWorker(),
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    claim_candidate = host.claim_candidate(
        request_id="checkpoint-expiration-claim",
        contract_id=contract.id,
        lease_seconds=1,
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract.id,
        lease_seconds=1,
        claim_candidate=claim_candidate,
    )
    assert claimed is not None

    from aigineering import runtime as runtime_module

    real_datetime = datetime

    class FutureDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(seconds=5)

    monkeypatch.setattr(runtime_module, "datetime", FutureDatetime)
    recovery_publishers = CandidatePublisherRegistry(
        (("recovery.publish.v1", setup.publisher),)
    )
    assert process_expired_claims(store, candidate_publishers=recovery_publishers) == [
        contract.id
    ]

    snapshot = store.read_raw_allowance_checkpoint(contract.id)
    extinguishment = store.scan_runtime_records(record_type="allowance.extinguished")[
        -1
    ][1]
    assert extinguishment.payload["contract_id"] == contract.id
    assert snapshot.status == "valid"
    assert snapshot.totals.extinguished_total == 5
    assert snapshot.totals.granted_total == 5
    assert store.audit_allowance_checkpoints() == ()
    store.close()


def test_every_relation_query_scales_with_fixed_tail_for_all_R_and_H(
    tmp_path: Path, monkeypatch
):
    """Raw query-pressure fixture: these rows are not admitted business facts."""
    relation_cases = (
        ("allowance.granted", "contract_id"),
        ("allowance.reserved", "source_contract_id"),
        ("allowance.returned", "source_contract_id"),
        ("allowance.returned", "from_contract_id"),
        ("allowance.extinguished", "contract_id"),
    )
    metrics = {}

    for relation_index, (record_type, field) in enumerate(relation_cases):
        for tail_count in (1, 2):
            vm_by_r = {}
            for historical_r in (0, 16, 64, 256):
                for irrelevant_h in (0, 16, 64):
                    path = tmp_path / (
                        f"relation-{relation_index}-tail-{tail_count}-"
                        f"r-{historical_r}-h-{irrelevant_h}.sqlite"
                    )
                    reader = SQLiteStore(str(path))
                    target = f"raw-query-account-{relation_index}"
                    other = f"raw-query-other-{relation_index}"
                    writer = SQLiteStore(
                        str(path), allowance_checkpoint_mode="reference"
                    )

                    def payload(amount: int) -> dict:
                        if record_type == "allowance.granted":
                            return {
                                "amount": amount,
                                "contract_id": target,
                                "grant_id": f"grant:{relation_index}:{amount}",
                                "purpose": "root",
                                "source_contract_id": "",
                            }
                        if record_type == "allowance.reserved":
                            return {
                                "amount": amount,
                                "child_contract_id": f"child:{amount}",
                                "purpose": "execution",
                                "reservation_id": f"reservation:{amount}",
                                "source_contract_id": target,
                            }
                        if record_type == "allowance.returned":
                            return {
                                "amount": amount,
                                "from_contract_id": (
                                    target if field == "from_contract_id" else other
                                ),
                                "source_contract_id": (
                                    target if field == "source_contract_id" else other
                                ),
                            }
                        return {"amount": amount, "contract_id": target}

                    try:
                        # Build a fixed indexed prefix. Multiple grants here are
                        # solely a raw SQL/fold pressure fixture, not an accepted
                        # declaration history or allowance authorization claim.
                        reader._allowance_checkpoint_mode = "reference"
                        with reader._conn:
                            for index in range(historical_r):
                                reader._insert_runtime_record(
                                    create_runtime_record(
                                        record_type,
                                        payload(index + 1),
                                        recorded_at=(
                                            f"2026-01-01T00:{index // 60:02d}:"
                                            f"{index % 60:02d}Z"
                                        ),
                                    )
                                )
                            for index in range(irrelevant_h):
                                reader._insert_runtime_record(
                                    create_runtime_record(
                                        "raw.query.background",
                                        {"ordinal": index},
                                        recorded_at=(
                                            f"2026-02-01T00:{index // 60:02d}:"
                                            f"{index % 60:02d}Z"
                                        ),
                                    )
                                )
                        reader._allowance_checkpoint_mode = "checkpoint"
                        reader.rebuild_allowance_checkpoints()
                        checkpoint = _checkpoint_row(reader, target)
                        cursor = (
                            0
                            if checkpoint is None
                            else int(checkpoint["through_revision"])
                        )

                        # A separate reference-mode Store stands in for an
                        # append-only writer that does not maintain this cache.
                        with writer._conn:
                            for index in range(tail_count):
                                writer._insert_runtime_record(
                                    create_runtime_record(
                                        record_type,
                                        payload(10_000 + index),
                                        recorded_at=(f"2026-03-01T00:00:{index:02d}Z"),
                                    )
                                )

                        decoded = []
                        fold_inputs = []
                        original_decode = reader._row_to_runtime_record
                        original_fold = sqlite_store_module.fold_raw_allowance_records

                        def decode(row):
                            decoded.append(row["record_id"])
                            return original_decode(row)

                        def fold(contract_id, records):
                            rows = tuple(records)
                            fold_inputs.append(len(rows))
                            return original_fold(contract_id, rows)

                        monkeypatch.setattr(reader, "_row_to_runtime_record", decode)
                        monkeypatch.setattr(
                            sqlite_store_module, "fold_raw_allowance_records", fold
                        )
                        callbacks = [0]

                        def progress():
                            callbacks[0] += 1
                            return 0

                        reader._conn.set_progress_handler(progress, 1)
                        try:
                            result = reader.read_raw_allowance_checkpoint(target)
                        finally:
                            reader._conn.set_progress_handler(None, 0)
                        vm_by_r.setdefault(historical_r, []).append(callbacks[0])

                        rows_sql = (
                            "SELECT * FROM runtime_records WHERE record_type=? "
                            f"AND json_extract(payload_json, '$.{field}')=? "
                            "AND revision>? AND revision<=? ORDER BY revision"
                        )
                        tail_rows = reader._conn.execute(
                            rows_sql,
                            (
                                record_type,
                                target,
                                cursor,
                                reader.get_runtime_revision(),
                            ),
                        ).fetchall()
                        raw_rows_by_relation = {}
                        for measured_type, measured_field in relation_cases:
                            measured_sql = (
                                "SELECT * FROM runtime_records WHERE record_type=? "
                                f"AND json_extract(payload_json, '$.{measured_field}')=? "
                                "AND revision>? AND revision<=? ORDER BY revision"
                            )
                            raw_rows_by_relation[
                                f"{measured_type}:{measured_field}"
                            ] = len(
                                reader._conn.execute(
                                    measured_sql,
                                    (
                                        measured_type,
                                        target,
                                        cursor,
                                        reader.get_runtime_revision(),
                                    ),
                                ).fetchall()
                            )
                        expected_rows = tail_count
                        assert result.status == "valid"
                        assert result.tail_record_count == expected_rows
                        assert result.decoded_record_count == expected_rows
                        assert result.fold_record_count == expected_rows
                        assert len(tail_rows) == expected_rows
                        assert len(decoded) == expected_rows
                        assert fold_inputs == [expected_rows]
                        plan = (
                            reader._conn.execute(
                                "EXPLAIN QUERY PLAN " + rows_sql,
                                (
                                    record_type,
                                    target,
                                    cursor,
                                    reader.get_runtime_revision(),
                                ),
                            )
                            .fetchone()["detail"]
                            .lower()
                        )
                        assert "search runtime_records" in plan
                        assert "rowid>?" in plan and "rowid<?" in plan
                        metrics[
                            (record_type, field, tail_count, historical_r, irrelevant_h)
                        ] = {
                            "raw_sql_rows_by_relation": raw_rows_by_relation,
                            "raw_sql_rows_total": sum(raw_rows_by_relation.values()),
                            "returned": len(tail_rows),
                            "decoded": len(decoded),
                            "folded": fold_inputs[0],
                            "vm_callbacks": callbacks[0],
                            "plan": plan,
                        }
                    finally:
                        writer.close()
                        reader.close()
                        monkeypatch.undo()

            baseline = vm_by_r[0]
            for historical_r in (16, 64, 256):
                assert max(vm_by_r[historical_r]) <= 2 * max(baseline) + 64

    metrics_path = tmp_path / "allowance-relation-matrix.json"
    import json

    metrics_path.write_text(
        json.dumps(
            {"|".join(map(str, key)): value for key, value in metrics.items()},
            sort_keys=True,
            indent=2,
        )
    )
    assert len(metrics) == 5 * 2 * 4 * 3
