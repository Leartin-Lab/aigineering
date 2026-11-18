import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aigineering.core.commitment import reduce_candidate
from aigineering.core.allowance_publication_index import (
    AllowanceBalanceInput,
    DeclarationPairsRequest,
    PublicationClosureRequest,
    PublicationHeadsRequest,
)
from aigineering.core.ids import contract_identity_v3
from aigineering.core.candidate_publisher import publish_effects
from aigineering.core.signing import create_verifier
from aigineering.core.causal_allowance import (
    RawAllowanceTotals,
    project_contract_allowance_records,
    validate_allowance_commit,
)
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.candidate import create_candidate_proposal
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.projection_context import (
    load_contract_declaration_projection_context,
)
from aigineering.protocol.types import Contract
from aigineering.protocol.wire import contract_to_dict
from conftest import candidate_runtime


def _identified(contract):
    return replace(contract, id=contract_identity_v3(contract))


def _uncommitted_declaration(store, runtime, contract, key):
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(contract_declaration_effect(contract),),
        signer=runtime.signer,
        idempotency_key=key,
    )
    decision = reduce_candidate(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        projection_context=load_contract_declaration_projection_context(
            store, candidate
        ),
    )
    assert decision.accepted, decision
    return candidate, decision


def _assert_same_failure(left, right):
    assert _capture_failure(left) == _capture_failure(right)


def _capture_failure(call):
    try:
        call()
    except Exception as exc:
        return type(exc), str(exc)
    raise AssertionError("expected allowance proof rejection")


def test_typed_publication_reads_select_exact_heads_closure_and_declaration_pairs():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=12)))
    records = tuple(record for _, record in store.scan_runtime_records())
    reservation = next(
        record
        for record in records
        if record.record_type == "allowance.granted"
        and record.payload.get("contract_id") == parent.id
    )
    declaration = next(
        record
        for record in records
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == parent.id
    )
    candidate_id = declaration.payload["candidate_id"]

    heads = store.read_allowance_publication_selection(
        PublicationHeadsRequest((reservation.id,))
    )
    assert heads is not None and heads.complete
    assert len(heads.records) == 1
    head = heads.records[0]
    assert reservation.id in head.payload["committed_record_ids"]

    closure = store.read_allowance_publication_selection(
        PublicationClosureRequest(
            (head.id, *head.payload["committed_record_ids"], *head.causal_parents)
        )
    )
    assert closure is not None and closure.complete
    closure_ids = {record.id for record in closure.records}
    assert {head.id, reservation.id, declaration.id}.issubset(closure_ids)

    declarations = store.read_allowance_publication_selection(
        DeclarationPairsRequest(((candidate_id, parent.id),))
    )
    assert declarations is not None and declarations.complete
    assert tuple(record.id for record in declarations.records) == (declaration.id,)
    store.close()


def test_legacy_selected_inputs_without_new_balance_field_remain_compatible():
    class LegacyStore:
        def read_contract_declaration_projection_inputs(self, _request):
            return SimpleNamespace(contracts=(), assets=(), runtime_records=())

        def get_all_contracts(self):
            raise AssertionError("legacy selected reader should be used")

        def get_all_assets(self):
            raise AssertionError("legacy selected reader should be used")

        def scan_runtime_records(self):
            raise AssertionError("legacy selected reader should be used")

        def get_worker_registrations(self):
            raise AssertionError("legacy selected reader should be used")

        def get_claim_history_contract_ids(self):
            raise AssertionError("legacy selected reader should be used")

    candidate = SimpleNamespace(
        effects=(
            SimpleNamespace(
                effect_type="contract.declare",
                payload={
                    "contract": contract_to_dict(_identified(Contract(id="", budget=2)))
                },
            ),
        ),
        claim_binding=None,
        id="candidate:legacy",
    )

    context = load_contract_declaration_projection_context(LegacyStore(), candidate)

    assert context.allowance_balance_inputs is None


def test_many_source_balance_inputs_are_indexed_once_per_projection_and_writer():
    source_count = 32
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    sources = tuple(
        _identified(Contract(id="", name=f"wide-source-{index}", budget=3))
        for index in range(source_count)
    )
    source_publication = publish_effects(
        store,
        store,
        runtime.genesis,
        runtime.actor_key,
        runtime.signer,
        tuple(contract_declaration_effect(source) for source in sources),
        idempotency_key="wide-source-batch",
    )
    assert source_publication.accepted, source_publication
    children = tuple(
        _identified(
            Contract(
                id="",
                name=f"wide-child-{index}",
                parent_id=source.id,
                budget=1,
            )
        )
        for index, source in enumerate(sources)
    )
    prior_records = tuple(record for _, record in store.scan_runtime_records())
    totals = tuple(RawAllowanceTotals(1, 3, 0, 0, 0, 0) for _ in range(source_count))

    class CountedTuple(tuple):
        def __new__(cls, values):
            instance = super().__new__(cls, values)
            instance.items_visited = 0
            return instance

        def __iter__(self):
            self.items_visited += len(self)
            return super().__iter__()

    projection_balances = CountedTuple(
        AllowanceBalanceInput(source.id, balance)
        for source, balance in zip(sources, totals, strict=True)
    )
    projected = project_contract_allowance_records(
        children,
        sources,
        (),
        causal_parent="candidate:wide-projection",
        balance_inputs=projection_balances,
    )
    assert len(projected) == source_count * 2
    assert projection_balances.items_visited == source_count

    writer_publication = publish_effects(
        store,
        store,
        runtime.genesis,
        runtime.actor_key,
        runtime.signer,
        tuple(contract_declaration_effect(child) for child in children),
        idempotency_key="wide-child-batch",
    )
    assert writer_publication.accepted, writer_publication
    writer_balances = CountedTuple(
        AllowanceBalanceInput(source.id, balance)
        for source, balance in zip(sources, totals, strict=True)
    )
    validate_allowance_commit(
        (*sources, *writer_publication.contracts),
        prior_records,
        writer_publication.runtime_records,
        candidate_id=writer_publication.candidate_id,
        balance_inputs=writer_balances,
    )
    assert writer_balances.items_visited == source_count
    store.close()


def test_wide_signed_writer_selection_respects_connection_variable_limit():
    source_count = 128
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    sources = tuple(
        _identified(Contract(id="", name=f"limited-source-{index}", budget=3))
        for index in range(source_count)
    )
    source_publication = publish_effects(
        store,
        store,
        runtime.genesis,
        runtime.actor_key,
        runtime.signer,
        tuple(contract_declaration_effect(source) for source in sources),
        idempotency_key="limited-source-batch",
    )
    assert source_publication.accepted, source_publication
    children = tuple(
        _identified(
            Contract(
                id="",
                name=f"limited-child-{index}",
                parent_id=source.id,
                budget=1,
            )
        )
        for index, source in enumerate(source_publication.contracts)
    )
    selections = []
    original = store._narrow_allowance_commit_inputs

    def capture(records, candidate_id, **kwargs):
        selected = original(records, candidate_id, **kwargs)
        selections.append(selected)
        return selected

    store._narrow_allowance_commit_inputs = capture
    store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 199)
    publication = publish_effects(
        store,
        store,
        runtime.genesis,
        runtime.actor_key,
        runtime.signer,
        tuple(contract_declaration_effect(child) for child in children),
        idempotency_key="limited-child-batch",
    )

    assert publication.accepted, publication
    assert len(selections) == 1
    assert all(selection is not None for selection in selections)
    expected_contract_ids = {source.id for source in source_publication.contracts} | {
        child.id for child in children
    }
    expected_source_ids = {source.id for source in source_publication.contracts}
    assert all(
        {contract.id for contract in selection[0]} == expected_contract_ids
        for selection in selections
    )
    assert all(
        {balance.contract_id for balance in selection[2]} == expected_source_ids
        for selection in selections
    )
    assert {contract.parent_id for contract in publication.contracts} == {
        source.id for source in source_publication.contracts
    }
    assert {
        record.payload["source_contract_id"]
        for record in publication.runtime_records
        if record.record_type == "allowance.reserved"
    } == expected_source_ids
    assert store.audit_publication_index() == ()
    store.close()


def test_reference_mode_declines_indexed_publication_selection():
    store = SQLiteStore(":memory:", allowance_checkpoint_mode="reference")
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=10)))
    records = tuple(record for _, record in store.scan_runtime_records())
    fact = next(
        record
        for record in records
        if record.record_type == "allowance.granted"
        and record.payload.get("contract_id") == parent.id
    )

    assert (
        store.read_allowance_publication_selection(PublicationHeadsRequest((fact.id,)))
        is None
    )
    store.close()


def test_targeted_head_lookup_vm_work_is_bounded_by_unrelated_history():
    measurements = []
    for unrelated_heads in (0, 16, 64, 256):
        store = SQLiteStore(":memory:")
        runtime = candidate_runtime(store)
        target = None
        for index in range(unrelated_heads + 1):
            contract = _identified(Contract(id="", name=f"vm-head-{index}", budget=2))
            accepted = runtime.accept_contract(contract)
            if index == 0:
                target = accepted
        assert target is not None
        fact = next(
            record
            for _, record in store.scan_runtime_records()
            if record.record_type == "allowance.granted"
            and record.payload.get("contract_id") == target.id
        )
        steps = 0

        def count_steps():
            nonlocal steps
            steps += 1
            return 0

        store._conn.set_progress_handler(count_steps, 1)
        selection = store.read_allowance_publication_selection(
            PublicationHeadsRequest((fact.id,))
        )
        store._conn.set_progress_handler(None, 0)
        assert selection is not None and selection.complete
        assert len(selection.records) == 1
        measurements.append((unrelated_heads, steps))
        store.close()

    baseline = measurements[0][1]
    assert all(steps <= (2 * baseline) + 64 for _, steps in measurements)


def test_legacy_head_tail_lookup_vm_work_is_bounded_by_unrelated_prefix(tmp_path):
    measured = {}
    result_counts = {}
    for unrelated_heads in (0, 16, 64, 256):
        path = str(tmp_path / f"head-tail-{unrelated_heads}.sqlite")
        store = SQLiteStore(path)
        runtime = candidate_runtime(store)
        for index in range(unrelated_heads):
            runtime.accept_contract(
                _identified(Contract(id="", name=f"head-prefix-{index}", budget=1))
            )
        store.rebuild_publication_index()
        legacy = SQLiteStore(path, allowance_checkpoint_mode="reference")
        old_runtime = candidate_runtime(
            legacy,
            genesis=runtime.genesis,
            actor_key=runtime.actor_key,
            signer=runtime.signer,
        )
        old_runtime._sequence = runtime._sequence
        for tail_size in (1, 2):
            target = old_runtime.accept_contract(
                _identified(
                    Contract(
                        id="",
                        name=f"head-tail-{unrelated_heads}-{tail_size}",
                        budget=1,
                    )
                )
            )
            fact = next(
                record
                for _, record in legacy.scan_runtime_records(
                    record_type="allowance.granted"
                )
                if record.payload.get("contract_id") == target.id
            )
            steps = 0
            active_tail_query = {"value": False}
            tail_rows = {"value": 0}
            tail_decodes = {"value": 0}
            tail_statements = []
            original_row_factory = store._conn.row_factory
            original_decode = store._row_to_runtime_record

            def trace(statement):
                active = (
                    statement.startswith(
                        "SELECT * FROM runtime_records WHERE record_type='candidate.committed' "
                        "AND revision>"
                    )
                    and " AND revision<=" in statement
                    and statement.endswith(" ORDER BY revision")
                )
                active_tail_query["value"] = active
                if active:
                    tail_statements.append(statement)

            def count_row(cursor, row):
                if active_tail_query["value"]:
                    tail_rows["value"] += 1
                return sqlite3.Row(cursor, row)

            def count_decode(row):
                if active_tail_query["value"]:
                    tail_decodes["value"] += 1
                return original_decode(row)

            def count_steps():
                nonlocal steps
                if active_tail_query["value"]:
                    steps += 1
                return 0

            store._conn.row_factory = count_row
            store._row_to_runtime_record = count_decode
            store._conn.set_trace_callback(trace)
            store._conn.set_progress_handler(count_steps, 1)
            try:
                selected = store.read_allowance_publication_selection(
                    PublicationHeadsRequest((fact.id,))
                )
            finally:
                store._conn.set_progress_handler(None, 0)
                store._conn.set_trace_callback(None)
                store._conn.row_factory = original_row_factory
                store._row_to_runtime_record = original_decode
            assert selected is not None and selected.complete
            assert len(selected.records) == 1
            assert fact.id in selected.records[0].payload["committed_record_ids"]
            assert len(tail_statements) == 1
            assert tail_rows["value"] == tail_size
            assert tail_decodes["value"] >= tail_size
            assert steps > 0
            measured[(unrelated_heads, tail_size)] = steps
            result_counts[(unrelated_heads, tail_size)] = (
                tail_rows["value"],
                tail_decodes["value"],
            )
        plan = store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM runtime_records "
            "WHERE record_type='candidate.committed' AND revision>? AND revision<=? "
            "ORDER BY revision",
            (0, 1),
        ).fetchall()
        details = " ".join(str(row[3]) for row in plan)
        assert "idx_runtime_records_type_revision" in details
        assert "record_type=? AND revision>? AND revision<?" in details
        legacy.close()
        store.close()

    for tail_size in (1, 2):
        baseline = measured[(0, tail_size)]
        decoded_baseline = result_counts[(0, tail_size)][1]
        assert all(
            measured[(history, tail_size)] <= 2 * baseline + 64
            for history in (16, 64, 256)
        )
        assert all(
            result_counts[(history, tail_size)][0] == tail_size
            for history in (0, 16, 64, 256)
        )
        assert all(
            result_counts[(history, tail_size)][1] == decoded_baseline
            for history in (16, 64, 256)
        )


def test_legacy_writer_tail_is_selected_then_folded_on_next_current_write(tmp_path):
    path = str(tmp_path / "old-writer.sqlite")
    store = SQLiteStore(path)
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=30)))
    legacy = SQLiteStore(path, allowance_checkpoint_mode="reference")
    old_runtime = candidate_runtime(
        legacy,
        genesis=runtime.genesis,
        actor_key=runtime.actor_key,
        signer=runtime.signer,
    )
    old_runtime._sequence = runtime._sequence
    old_child = old_runtime.accept_contract(
        _identified(Contract(id="", parent_id=parent.id, budget=5))
    )
    old_grant = next(
        record
        for _, record in legacy.scan_runtime_records()
        if record.record_type == "allowance.granted"
        and record.payload.get("contract_id") == old_child.id
    )
    old_cursor = store._publication_index_state()["through_revision"]
    assert store.audit_publication_index() == ()

    tail = store.read_allowance_publication_selection(
        PublicationHeadsRequest((old_grant.id,))
    )
    assert tail is not None and len(tail.records) == 1
    assert old_grant.id in tail.records[0].payload["committed_record_ids"]
    assert store._publication_index_state()["through_revision"] == old_cursor

    runtime._sequence = old_runtime._sequence
    runtime.accept_contract(_identified(Contract(id="", parent_id=parent.id, budget=4)))
    assert store.audit_publication_index() == ()
    legacy.close()
    store.close()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("format_version", 99),
        ("through_revision", -1),
        ("basis_record_id", "record:wrong-basis"),
    ),
)
def test_invalid_publication_index_state_refuses_selection_without_repair(field, value):
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(_identified(Contract(id="", budget=6)))
    fact = next(
        record
        for _, record in store.scan_runtime_records()
        if record.record_type == "allowance.granted"
        and record.payload.get("contract_id") == contract.id
    )
    store._conn.execute(
        f"UPDATE publication_index_state SET {field}=? WHERE singleton=1", (value,)
    )
    store._conn.commit()

    assert (
        store.read_allowance_publication_selection(PublicationHeadsRequest((fact.id,)))
        is None
    )
    assert store._publication_index_state()[field] == value
    assert store.audit_publication_index()
    store.close()


def test_required_sql_indexes_cover_declaration_member_and_revision_selectors():
    store = SQLiteStore(":memory:")
    declaration = "SELECT * FROM runtime_records WHERE record_type='contract.declared' "
    declaration += "AND json_type(payload_json,'$.candidate_id')='text' "
    declaration += "AND json_extract(payload_json,'$.candidate_id')<>'' "
    declaration += "AND json_type(payload_json,'$.contract')='object' "
    declaration += "AND json_type(payload_json,'$.contract.id')='text' "
    declaration += "AND json_extract(payload_json,'$.contract.id')<>'' "
    declaration += "AND json_extract(payload_json,'$.candidate_id')=? "
    declaration += "AND json_extract(payload_json,'$.contract.id')=?"
    plans = (
        tuple(
            row[3]
            for row in store._conn.execute(
                "EXPLAIN QUERY PLAN " + declaration, ("candidate", "contract")
            ).fetchall()
        ),
        tuple(
            row[3]
            for row in store._conn.execute(
                "EXPLAIN QUERY PLAN SELECT head_record_id FROM publication_head_members "
                "INDEXED BY idx_publication_head_members_member WHERE member_id=?",
                ("member",),
            ).fetchall()
        ),
        tuple(
            row[3]
            for row in store._conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM runtime_records "
                "WHERE record_type='candidate.committed' AND revision>? AND revision<=?",
                (0, 1),
            ).fetchall()
        ),
    )

    assert "idx_runtime_records_declaration_pair" in str(plans[0])
    assert "idx_publication_head_members_member" in str(plans[1])
    assert "idx_runtime_records_type_revision" in str(plans[2])
    store.close()
