import sqlite3
from dataclasses import replace
from types import MappingProxyType
from types import SimpleNamespace

import pytest

from aigineering.core.commitment import CandidateCommitter, reduce_candidate
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.allowance_publication_index import (
    AllowanceBalanceInput,
    build_canonical_allowance_declaration_batch,
    DeclarationPairsRequest,
    PublicationClosureRequest,
    PublicationHeadsRequest,
    publication_closure_for_funding_batch,
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
from aigineering.protocol.candidate import CandidateEffect, create_candidate_proposal
from aigineering.protocol.runtime_record import create_runtime_record
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


def test_candidate_writer_uses_narrow_allowance_inputs_and_preserves_balance():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=20)))
    runtime.publisher = CandidatePublisher(
        store,
        store,
        runtime.genesis,
        runtime.actor_key,
        runtime.signer,
        additional_effects={"unused.test.effect": ("test.unused", lambda *_: ())},
    )
    original = store._narrow_allowance_commit_inputs
    selections = []
    original_reader = store.read_contract_declaration_projection_inputs
    projection_inputs = []
    original_selector = store.read_allowance_publication_selection
    request_types = []
    raw_balance_reads = []
    original_raw_balance_reader = store.read_raw_allowance_checkpoint

    def capture(records, candidate_id, **kwargs):
        selected = original(records, candidate_id, **kwargs)
        selections.append(selected)
        return selected

    store._narrow_allowance_commit_inputs = capture

    def capture_projection(request):
        selected = original_reader(request)
        projection_inputs.append(selected)
        return selected

    def capture_request(request):
        request_types.append(type(request))
        return original_selector(request)

    def capture_raw_balance(contract_id):
        raw_balance_reads.append(contract_id)
        return original_raw_balance_reader(contract_id)

    store.read_contract_declaration_projection_inputs = capture_projection
    store.read_allowance_publication_selection = capture_request
    store.read_raw_allowance_checkpoint = capture_raw_balance
    child = runtime.accept_contract(
        _identified(Contract(id="", parent_id=parent.id, budget=7))
    )

    assert len(selections) == 2  # Both existing writer validations are fresh.
    assert all(selection is not None for selection in selections)
    assert (
        request_types
        == [
            PublicationHeadsRequest,
            PublicationClosureRequest,
            DeclarationPairsRequest,
            PublicationHeadsRequest,
            PublicationClosureRequest,
        ]
        * 2
    )
    assert selections[0][2][0].contract_id == parent.id
    assert selections[0][2][0].totals.granted_total == 20
    # One projection capture and two independent, fresh writer validations.
    assert raw_balance_reads == [parent.id, parent.id, parent.id]
    child_inputs = projection_inputs[-1]
    assert child_inputs.allowance_balance_inputs[0].contract_id == parent.id
    assert child_inputs.allowance_balance_inputs[0].totals.granted_total == 20
    assert not any(
        record.record_type.startswith("allowance.")
        for record in child_inputs.runtime_records
    )
    assert child.parent_id == parent.id
    assert store.audit_publication_index() == ()
    store.close()


def test_narrow_writer_declines_pending_id_payload_conflict():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=20)))
    captured = {}
    original = store._narrow_allowance_commit_inputs

    def capture(records, candidate_id, **kwargs):
        captured.update(records=records, candidate_id=candidate_id, **kwargs)
        return original(records, candidate_id, **kwargs)

    store._narrow_allowance_commit_inputs = capture
    child = runtime.accept_contract(
        _identified(Contract(id="", parent_id=parent.id, budget=3))
    )
    batch = captured["records"]
    candidate_id = captured["candidate_id"]
    kwargs = {
        "candidate_proposal": captured["candidate_proposal"],
        "effect_fact_owners": captured["effect_fact_owners"],
    }
    target_grant = next(
        record
        for record in batch
        if record.record_type == "allowance.granted"
        and record.payload.get("contract_id") == child.id
    )
    conflict = replace(
        target_grant,
        payload={
            **dict(target_grant.payload),
            "amount": target_grant.payload["amount"] + 1,
        },
    )
    pending = tuple(
        conflict if record.id == conflict.id else record for record in batch
    )

    assert original(pending, candidate_id, **kwargs) is None
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


def test_synthetic_declaration_only_foreign_head_reaches_original_uniqueness_fence():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=8)))
    child = _identified(Contract(id="", parent_id=parent.id, budget=2))
    candidate, decision = _uncommitted_declaration(
        store, runtime, child, "synthetic-foreign-declaration-head"
    )
    declaration = next(
        record
        for record in decision.runtime_records
        if record.record_type == "contract.declared"
    )
    foreign_receipt = create_runtime_record(
        "candidate.received", {"candidate_id": "candidate:foreign-declaration"}
    )
    foreign_head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": "candidate:foreign-declaration",
            "committed_record_ids": [declaration.id],
        },
        causal_parents=(foreign_receipt.id, declaration.id),
    )
    with store._conn:
        store._insert_runtime_record(foreign_receipt)
        store._insert_runtime_record(foreign_head)

    narrow = store._narrow_allowance_commit_inputs(
        decision.runtime_records,
        candidate.id,
        candidate_proposal=candidate,
        effect_fact_owners=decision.effect_fact_owners,
    )
    assert narrow is not None
    assert foreign_head.id in {record.id for record in narrow[1]}
    existing = tuple(record for _, record in store.scan_runtime_records())
    _assert_same_failure(
        lambda: validate_allowance_commit(
            (parent, child),
            existing,
            decision.runtime_records,
            candidate_id=candidate.id,
        ),
        lambda: validate_allowance_commit(
            narrow[0],
            narrow[1],
            decision.runtime_records,
            candidate_id=candidate.id,
            balance_inputs=narrow[2],
        ),
    )
    store.close()


def test_synthetic_extra_direct_parent_receipt_is_in_narrow_closure():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    contract = _identified(Contract(id="", budget=4))
    candidate, decision = _uncommitted_declaration(
        store, runtime, contract, "synthetic-extra-root-receipt"
    )
    store.rebuild_publication_index()
    original_grant = next(
        record
        for record in decision.runtime_records
        if record.record_type == "allowance.granted"
    )
    original_head = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.committed"
    )
    extra_receipt = create_runtime_record(
        "candidate.received", {"candidate_id": "candidate:foreign-root-receipt"}
    )
    grant = create_runtime_record(
        original_grant.record_type,
        original_grant.payload,
        causal_parents=(*original_grant.causal_parents, extra_receipt.id),
    )
    head = create_runtime_record(
        original_head.record_type,
        {
            **dict(original_head.payload),
            "committed_record_ids": [
                grant.id if record_id == original_grant.id else record_id
                for record_id in original_head.payload["committed_record_ids"]
            ],
        },
        causal_parents=tuple(
            grant.id if record_id == original_grant.id else record_id
            for record_id in original_head.causal_parents
        ),
    )
    pending = tuple(
        grant
        if record.id == original_grant.id
        else head
        if record.id == original_head.id
        else record
        for record in decision.runtime_records
    )
    with store._conn:
        store._insert_runtime_record(extra_receipt)

    narrow = store._narrow_allowance_commit_inputs(
        pending,
        candidate.id,
        candidate_proposal=candidate,
        effect_fact_owners=decision.effect_fact_owners,
    )
    assert narrow is not None
    assert extra_receipt.id in {record.id for record in narrow[1]}
    existing = tuple(record for _, record in store.scan_runtime_records())
    _assert_same_failure(
        lambda: validate_allowance_commit(
            (contract,), existing, pending, candidate_id=candidate.id
        ),
        lambda: validate_allowance_commit(
            narrow[0],
            narrow[1],
            pending,
            candidate_id=candidate.id,
            balance_inputs=narrow[2],
        ),
    )
    store.close()


def test_synthetic_foreign_funding_head_and_global_opaque_use_reference_fallback():
    for mode in ("foreign", "opaque"):
        store = SQLiteStore(":memory:")
        runtime = candidate_runtime(store)
        parent = runtime.accept_contract(_identified(Contract(id="", budget=8)))
        child = _identified(Contract(id="", parent_id=parent.id, budget=2))
        candidate, decision = _uncommitted_declaration(
            store, runtime, child, f"synthetic-{mode}-funding-history"
        )
        if mode == "foreign":
            fact = next(
                record
                for record in decision.runtime_records
                if record.record_type == "allowance.granted"
                and record.payload.get("contract_id") == child.id
            )
            receipt = create_runtime_record(
                "candidate.received", {"candidate_id": "candidate:foreign-funding"}
            )
            extra_head = create_runtime_record(
                "candidate.committed",
                {
                    "candidate_id": "candidate:foreign-funding",
                    "committed_record_ids": [fact.id],
                },
                causal_parents=(receipt.id, fact.id),
            )
            with store._conn:
                store._insert_runtime_record(receipt)
                store._insert_runtime_record(extra_head)
        else:
            extra_head = create_runtime_record(
                "candidate.committed",
                {"candidate_id": "candidate:opaque", "committed_record_ids": None},
            )
            with store._conn:
                store._insert_runtime_record(extra_head)

        assert (
            store._narrow_allowance_commit_inputs(
                decision.runtime_records,
                candidate.id,
                candidate_proposal=candidate,
                effect_fact_owners=decision.effect_fact_owners,
            )
            is None
        )
        full_records = tuple(record for _, record in store.scan_runtime_records())
        assert _capture_failure(
            lambda: validate_allowance_commit(
                (parent, child),
                full_records,
                decision.runtime_records,
                candidate_id=candidate.id,
            ),
        )[0]
        store.close()


def test_legacy_source_terminal_falls_back_before_text_id_query():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    legacy_source = Contract(id="1", budget=5)
    store.add_contract(legacy_source)
    terminal = create_runtime_record(
        "lifecycle.terminal", {"contract_id": 1, "terminal": "failed"}
    )
    with store._conn:
        store._insert_runtime_record(terminal)
    child = _identified(
        Contract(id="", name="legacy-source-child", parent_id="1", budget=1)
    )
    candidate, decision = _uncommitted_declaration(
        store, runtime, child, "legacy-source-terminal-fallback"
    )

    assert (
        store._narrow_allowance_commit_inputs(
            decision.runtime_records,
            candidate.id,
            candidate_proposal=candidate,
            effect_fact_owners=decision.effect_fact_owners,
        )
        is None
    )
    with pytest.raises(ValueError, match="terminal Contract"):
        validate_allowance_commit(
            (legacy_source, child),
            tuple(record for _, record in store.scan_runtime_records()),
            decision.runtime_records,
            candidate_id=candidate.id,
        )
    store.close()


def test_malformed_pending_head_falls_back_before_earlier_grant_error():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    contract = _identified(Contract(id="", budget=2))
    candidate, decision = _uncommitted_declaration(
        store, runtime, contract, "malformed-pending-head-before-grant-failure"
    )
    original_grant = next(
        record
        for record in decision.runtime_records
        if record.record_type == "allowance.granted"
    )
    original_head = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.committed"
    )
    malformed_grant = create_runtime_record(
        original_grant.record_type,
        {**dict(original_grant.payload), "amount": 3},
        causal_parents=original_grant.causal_parents,
    )
    malformed_head = create_runtime_record(
        original_head.record_type,
        {
            **dict(original_head.payload),
            "committed_record_ids": None,
        },
        causal_parents=tuple(
            malformed_grant.id if item == original_grant.id else item
            for item in original_head.causal_parents
        ),
    )
    pending = tuple(
        malformed_grant
        if record.id == original_grant.id
        else malformed_head
        if record.id == original_head.id
        else record
        for record in decision.runtime_records
    )

    assert (
        store._narrow_allowance_commit_inputs(
            pending,
            candidate.id,
            candidate_proposal=candidate,
            effect_fact_owners=decision.effect_fact_owners,
        )
        is None
    )
    with pytest.raises(
        ValueError, match="grant must match its declared Contract budget"
    ):
        validate_allowance_commit((contract,), (), pending, candidate_id=candidate.id)
    store.close()


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
    assert len(selections) == 2
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


def test_writer_selection_rejects_unusable_variable_limit_without_partial_commit():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(_identified(Contract(id="", budget=3)))
    child = _identified(Contract(id="", parent_id=parent.id, budget=1))
    candidate, decision = _uncommitted_declaration(
        store, runtime, child, "unqueryable-child"
    )
    before_records = tuple(store.scan_runtime_records())
    store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 1)

    with pytest.raises(sqlite3.OperationalError, match="variable limit"):
        store._narrow_allowance_commit_inputs(
            decision.runtime_records,
            candidate.id,
            candidate_proposal=candidate,
            effect_fact_owners=decision.effect_fact_owners,
        )

    assert tuple(store.scan_runtime_records()) == before_records
    store.close()


def test_writer_existing_candidate_guard_uses_partial_indexes_with_fixed_vm_cost():
    vm_by_history = {}
    for unrelated_count in (0, 16, 64):
        store = SQLiteStore(":memory:")
        runtime = candidate_runtime(store)
        parent = runtime.accept_contract(
            _identified(Contract(id="", name="candidate-guard-parent", budget=2))
        )
        for index in range(unrelated_count):
            runtime.accept_contract(
                _identified(
                    Contract(
                        id="",
                        name=f"candidate-guard-unrelated-{index}",
                        budget=1,
                    )
                )
            )
        candidate, decision = _uncommitted_declaration(
            store,
            runtime,
            _identified(
                Contract(
                    id="", name="candidate-guard-child", parent_id=parent.id, budget=1
                )
            ),
            f"candidate-guard-{unrelated_count}",
        )
        current_sql = {"target": False}
        statements = []
        vm_steps = {"target": 0}

        def trace(statement):
            is_target = (
                "SELECT 1 FROM runtime_records WHERE " in statement
                and "candidate.received" in statement
                and "candidate.committed" in statement
                and "json_extract(payload_json,'$.candidate_id')=" in statement
            )
            current_sql["target"] = is_target
            if is_target:
                statements.append(statement)

        def progress():
            if current_sql["target"]:
                vm_steps["target"] += 1
            return 0

        store._conn.set_trace_callback(trace)
        store._conn.set_progress_handler(progress, 1)
        try:
            selected = store._narrow_allowance_commit_inputs(
                decision.runtime_records,
                candidate.id,
                candidate_proposal=candidate,
                effect_fact_owners=decision.effect_fact_owners,
            )
        finally:
            store._conn.set_progress_handler(None, 0)
            store._conn.set_trace_callback(None)

        assert selected is not None
        assert len(statements) == 1
        assert "record_type='candidate.received' AND" in statements[0]
        assert "record_type='candidate.committed' AND" in statements[0]
        assert vm_steps["target"] > 0
        vm_by_history[unrelated_count] = vm_steps["target"]
        plan = store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM runtime_records WHERE "
            "(record_type='candidate.received' AND "
            "json_extract(payload_json, '$.candidate_id')=?) OR "
            "(record_type='candidate.committed' AND "
            "json_extract(payload_json, '$.candidate_id')=?) LIMIT 1",
            (candidate.id, candidate.id),
        ).fetchall()
        details = " ".join(str(row[3]) for row in plan)
        assert "idx_runtime_records_received_candidate" in details
        assert "idx_runtime_records_committed_candidate" in details
        store.close()

    baseline = vm_by_history[0]
    assert all(vm_by_history[history] <= 2 * baseline + 64 for history in (16, 64))


def test_fixed_source_batch_work_is_stable_across_history_height():
    import collections
    import aigineering.core.sqlite_store as sqlite_store_module
    from aigineering.agent.mock import MockWorker
    from aigineering.runtime import claim_next_package, execute_claimed_package
    from conftest import hosted_worker

    parent_count = 8
    measurements = {}
    for history_count in (0, 16, 64):
        store = SQLiteStore(":memory:")
        runtime = candidate_runtime(store)
        sources = tuple(
            _identified(
                Contract(
                    id="",
                    name=f"matrix-source-{history_count}-{index}",
                    budget=3,
                )
            )
            for index in range(parent_count)
        )
        source_result = publish_effects(
            store,
            store,
            runtime.genesis,
            runtime.actor_key,
            runtime.signer,
            tuple(contract_declaration_effect(source) for source in sources),
            idempotency_key=f"matrix-sources-{history_count}",
        )
        assert source_result.accepted, source_result
        history_worker = MockWorker(worker_id=f"matrix-history-worker-{history_count}")
        history_host = hosted_worker(
            store,
            history_worker,
            genesis=runtime.genesis,
            authority_key=runtime.actor_key,
            authority_signer=runtime.signer,
        )
        for index in range(history_count):
            output_name = f"history_result_{history_count}_{index}"
            history_contract = runtime.accept_contract(
                _identified(
                    Contract(
                        id="",
                        name=f"matrix-history-{history_count}-{index}",
                        outputs=(output_name,),
                        budget=1,
                    )
                )
            )
            history_worker.set_output(history_contract.name, f"{output_name}: closed")
            claim_candidate = history_host.claim_candidate(
                request_id=f"matrix-history-claim-{history_count}-{index}",
                contract_id=history_contract.id,
                lease_seconds=60,
            )
            claimed = claim_next_package(
                store,
                worker_id=history_host.worker_id,
                contract_id=history_contract.id,
                lease_seconds=60,
                claim_candidate=claim_candidate,
            )
            assert claimed is not None
            result = execute_claimed_package(claimed, history_host, store)
            assert result["accepted"] == [output_name]
        assert (
            sum(
                record.payload.get("terminal") == "complete"
                for _, record in store.scan_runtime_records(
                    record_type="lifecycle.terminal"
                )
            )
            == history_count
        )
        children = tuple(
            _identified(
                Contract(
                    id="",
                    name=f"matrix-child-{history_count}-{index}",
                    parent_id=source.id,
                    budget=1,
                )
            )
            for index, source in enumerate(source_result.contracts)
        )
        candidate = create_candidate_proposal(
            domain_id=runtime.genesis.id,
            actor_id=runtime.actor_key.actor_id,
            key_id=runtime.actor_key.key_id,
            effects=tuple(contract_declaration_effect(child) for child in children),
            signer=runtime.signer,
            idempotency_key=f"matrix-children-{history_count}",
        )
        prior_ids = {record.id for _, record in store.scan_runtime_records()}
        for source in source_result.contracts:
            checkpoint = store.read_raw_allowance_checkpoint(source.id)
            assert checkpoint.status == "valid"
            assert checkpoint.tail_record_count == 0

        phase = {"value": "projection"}
        phase_counts = collections.defaultdict(collections.Counter)
        raw_reads = []
        fold_inputs = collections.defaultdict(list)
        writer_selections = []
        set_trace_callback = store._conn.set_trace_callback
        original_row_factory = store._conn.row_factory
        original_decode = store._row_to_runtime_record
        original_raw_read = store.read_raw_allowance_checkpoint
        original_narrow = store._narrow_allowance_commit_inputs
        original_scan = store.scan_runtime_records
        original_all_contracts = store.get_all_contracts
        original_all_assets = store.get_all_assets
        original_insert = store._insert_runtime_record
        original_fold = sqlite_store_module.fold_raw_allowance_records

        def count_row(cursor, row):
            phase_counts[phase["value"]]["sql_rows_returned"] += 1
            return sqlite3.Row(cursor, row)

        def count_decode(row):
            phase_counts[phase["value"]]["runtime_records_decoded"] += 1
            return original_decode(row)

        def count_raw(contract_id):
            result = original_raw_read(contract_id)
            raw_reads.append((contract_id, result.tail_record_count))
            phase_counts[phase["value"]]["raw_checkpoint_reads"] += 1
            return result

        def capture_narrow(records, candidate_id, **kwargs):
            phase["value"] = "writer"
            selected = original_narrow(records, candidate_id, **kwargs)
            writer_selections.append(selected)
            return selected

        def count_fold(contract_id, records):
            materialized = tuple(records)
            fold_inputs[phase["value"]].append(
                (contract_id, tuple(record.id for record in materialized))
            )
            phase_counts[phase["value"]]["fold_calls"] += 1
            return original_fold(contract_id, materialized)

        def count_select(statement):
            if statement.lstrip().upper().startswith("SELECT"):
                phase_counts[phase["value"]]["select_statements"] += 1

        def count_insert(record, **kwargs):
            if record.record_type.startswith("allowance."):
                phase["value"] = "maintenance"
            return original_insert(record, **kwargs)

        def reject_full_scan(
            *, after_revision=0, through_revision=None, record_type=None
        ):
            if record_type is None:
                raise AssertionError("candidate writer requested a full runtime scan")
            return original_scan(
                after_revision=after_revision,
                through_revision=through_revision,
                record_type=record_type,
            )

        def reject_get_all():
            raise AssertionError("candidate writer requested a full table read")

        store._conn.row_factory = count_row
        store._row_to_runtime_record = count_decode
        store.read_raw_allowance_checkpoint = count_raw
        store._narrow_allowance_commit_inputs = capture_narrow
        store.scan_runtime_records = reject_full_scan
        store.get_all_contracts = reject_get_all
        store.get_all_assets = reject_get_all
        store._insert_runtime_record = count_insert
        set_trace_callback(count_select)
        sqlite_store_module.fold_raw_allowance_records = count_fold
        try:
            decision = CandidateCommitter(store, store).commit(
                candidate, runtime.genesis
            )
        finally:
            sqlite_store_module.fold_raw_allowance_records = original_fold
            set_trace_callback(None)
            store._conn.row_factory = original_row_factory
            store._row_to_runtime_record = original_decode
            store.read_raw_allowance_checkpoint = original_raw_read
            store._narrow_allowance_commit_inputs = original_narrow
            store.scan_runtime_records = original_scan
            store.get_all_contracts = original_all_contracts
            store.get_all_assets = original_all_assets
            store._insert_runtime_record = original_insert

        assert decision.accepted, decision
        assert len(writer_selections) == 2
        assert all(selection is not None for selection in writer_selections)
        assert len(raw_reads) == 3 * parent_count
        assert collections.Counter(contract_id for contract_id, _ in raw_reads) == {
            source.id: 3 for source in source_result.contracts
        }
        assert all(tail_count == 0 for _, tail_count in raw_reads)
        new_allowance_ids = {
            record.id
            for record in decision.runtime_records
            if record.record_type.startswith("allowance.")
        }
        actual_tail_folds = collections.Counter(
            (account_id, record_id)
            for account_id, record_ids in fold_inputs["maintenance"]
            for record_id in record_ids
        )
        expected_tail_folds = collections.Counter()
        for record in decision.runtime_records:
            if record.record_type in {"allowance.granted", "allowance.extinguished"}:
                accounts = (str(record.payload.get("contract_id", "")),)
            elif record.record_type == "allowance.reserved":
                accounts = (str(record.payload.get("source_contract_id", "")),)
            elif record.record_type == "allowance.returned":
                accounts = (
                    str(record.payload.get("source_contract_id", "")),
                    str(record.payload.get("from_contract_id", "")),
                )
            else:
                accounts = ()
            for account_id in set(accounts) - {""}:
                expected_tail_folds[(account_id, record.id)] += 1
        folded_ids = {record_id for _, record_id in actual_tail_folds}
        assert not (folded_ids & prior_ids)
        assert folded_ids <= new_allowance_ids
        assert actual_tail_folds == expected_tail_folds
        assert phase_counts["projection"]["raw_checkpoint_reads"] == parent_count
        assert phase_counts["writer"]["raw_checkpoint_reads"] == 2 * parent_count
        assert phase_counts["maintenance"]["raw_checkpoint_reads"] == 0
        assert phase_counts["projection"]["fold_calls"] == parent_count
        assert phase_counts["writer"]["fold_calls"] == 2 * parent_count
        assert phase_counts["projection"]["select_statements"] > 0
        assert phase_counts["writer"]["select_statements"] > 0
        assert phase_counts["maintenance"]["select_statements"] > 0
        for phase_name in ("projection", "writer", "maintenance"):
            assert phase_counts[phase_name]["sql_rows_returned"] > 0
            assert phase_counts[phase_name]["runtime_records_decoded"] > 0
        measurements[history_count] = {
            phase_name: tuple(sorted(values.items()))
            for phase_name, values in phase_counts.items()
        }
        measurements[history_count]["tail_fold_count"] = sum(actual_tail_folds.values())
        store.close()
    assert measurements[0] == measurements[16] == measurements[64]


@pytest.mark.parametrize("intervening_change", ("spend", "terminal"))
def test_stale_projection_is_rejected_by_fresh_writer_after_other_connection_change(
    tmp_path, monkeypatch, intervening_change
):
    """Two accepted old projections cannot bypass the SQLite writer recheck."""
    from aigineering.core.domain import initialize_genesis
    from aigineering.core.signing import Ed25519Signer
    from aigineering.protocol.candidate import ActorKey, create_genesis_manifest

    database = tmp_path / f"stale-projection-{intervening_change}.sqlite"
    first_store = SQLiteStore(str(database))
    signer = Ed25519Signer()
    actor = ActorKey(
        "test:two-writer-race",
        "two-writer-race-key",
        signer.kind,
        signer.signer_id,
        (
            "actor.authorize",
            "asset.publish",
            "asset.publish.protected",
            "asset.relate",
            "contract.publish",
            "contract.publish.protected",
            "contract.cancel",
            "worker.register",
        ),
    )
    genesis = create_genesis_manifest(
        "test-two-writer-projection-race", (actor,), "policy:test-two-writer-race"
    )
    initialize_genesis(first_store, genesis)
    runtime = candidate_runtime(
        first_store, genesis=genesis, actor_key=actor, signer=signer
    )
    parent = runtime.accept_contract(
        _identified(Contract(id="", name="two-writer-parent", budget=3))
    )
    second_store = SQLiteStore(str(database))

    def signed_candidate(effects, key):
        return create_candidate_proposal(
            domain_id=genesis.id,
            actor_id=actor.actor_id,
            key_id=actor.key_id,
            effects=effects,
            signer=signer,
            idempotency_key=key,
        )

    first_child = _identified(
        Contract(id="", name="first-race-child", parent_id=parent.id, budget=2)
    )
    second_child = _identified(
        Contract(id="", name="second-race-child", parent_id=parent.id, budget=2)
    )
    first_candidate = (
        signed_candidate(
            (contract_declaration_effect(first_child),), "first-spend-candidate"
        )
        if intervening_change == "spend"
        else signed_candidate(
            (
                CandidateEffect(
                    "contract.cancel",
                    {
                        "contract_id": parent.id,
                        "reason": "signed concurrent terminal change",
                    },
                ),
            ),
            "first-terminal-candidate",
        )
    )
    second_candidate = signed_candidate(
        (contract_declaration_effect(second_child),), "stale-second-child-candidate"
    )

    # Both connections capture the same pre-write state. Both pure decisions
    # therefore accept before either signed publication commits.
    contexts = {
        first_candidate.id: load_contract_declaration_projection_context(
            first_store, first_candidate
        ),
        second_candidate.id: load_contract_declaration_projection_context(
            second_store, second_candidate
        ),
    }
    old_decisions = tuple(
        reduce_candidate(
            candidate,
            genesis,
            verifier_factory=create_verifier,
            projection_context=contexts[candidate.id],
        )
        for candidate in (first_candidate, second_candidate)
    )
    assert all(decision.accepted for decision in old_decisions)
    stale_balance_inputs = contexts[second_candidate.id].allowance_balance_inputs
    assert stale_balance_inputs is not None
    assert len(stale_balance_inputs) == 1
    assert stale_balance_inputs[0].contract_id == parent.id
    assert stale_balance_inputs[0].totals.reserved_total == 0
    assert stale_balance_inputs[0].totals.extinguished_total == 0

    # Freeze only the initial reducer input. The two SQLite writers, their
    # transactions, caches, and narrow validation stay real and independent.
    import aigineering.core.commitment as commitment_module

    projection_context_module = commitment_module.context_inputs
    original_loader = projection_context_module.load_candidate_projection_context
    loader_calls = []

    def frozen_context_loader(store, candidate, **kwargs):
        context = contexts.get(candidate.id)
        if context is None:
            return original_loader(store, candidate, **kwargs)
        loader_calls.append((candidate.id, context))
        return context

    monkeypatch.setattr(
        projection_context_module,
        "load_candidate_projection_context",
        frozen_context_loader,
    )
    original_reduce = commitment_module.reduce_candidate
    preprojection_decisions = []

    def observe_reduce(candidate, *args, **kwargs):
        decision = original_reduce(candidate, *args, **kwargs)
        preprojection_decisions.append((candidate.id, decision.accepted))
        return decision

    monkeypatch.setattr(commitment_module, "reduce_candidate", observe_reduce)
    writer_events = []
    for label, store in (("first", first_store), ("second", second_store)):
        original_narrow = store._narrow_allowance_commit_inputs

        def capture_narrow(
            records,
            candidate_id,
            *,
            _original=original_narrow,
            _store=store,
            _label=label,
            **kwargs,
        ):
            selected = _original(records, candidate_id, **kwargs)
            writer_events.append(
                {
                    "writer": _label,
                    "in_transaction": _store._conn.in_transaction,
                    "selected": selected,
                    "balances": None if selected is None else selected[2],
                }
            )
            return selected

        monkeypatch.setattr(store, "_narrow_allowance_commit_inputs", capture_narrow)

    first_result = CandidateCommitter(first_store, first_store).commit(
        first_candidate, genesis
    )
    second_result = CandidateCommitter(second_store, second_store).commit(
        second_candidate, genesis
    )
    assert first_result.accepted, first_result
    assert not second_result.accepted, second_result
    assert loader_calls == [
        (first_candidate.id, contexts[first_candidate.id]),
        (second_candidate.id, contexts[second_candidate.id]),
    ]
    assert preprojection_decisions == [
        (first_candidate.id, True),
        (second_candidate.id, True),
    ]

    first_events = [event for event in writer_events if event["writer"] == "first"]
    second_events = [event for event in writer_events if event["writer"] == "second"]
    assert len(first_events) == 2
    assert all(event["in_transaction"] for event in (*first_events, *second_events))
    if intervening_change == "spend":
        assert all(event["selected"] is not None for event in first_events)
    assert len(second_events) == 1
    second_event = second_events[0]
    assert second_event["selected"] is not None
    assert len(second_event["balances"]) == 1
    fresh_balance = second_event["balances"][0]
    assert fresh_balance.contract_id == parent.id
    if intervening_change == "spend":
        assert fresh_balance.totals.reserved_total == 2
        assert fresh_balance.totals.extinguished_total == 0
    else:
        assert fresh_balance.totals.reserved_total == 0
        assert fresh_balance.totals.extinguished_total == 3

    assert second_store.get_contract(second_child.id) is None
    reservations = second_store.scan_runtime_records(record_type="allowance.reserved")
    assert not any(
        record.payload.get("child_contract_id") == second_child.id
        for _, record in reservations
    )
    rejected = second_store.scan_runtime_records(record_type="candidate.rejected")
    assert len(rejected) == 1
    assert rejected[0][1].payload["candidate_id"] == second_candidate.id
    rejection_reason = rejected[0][1].payload["reason"]
    if intervening_change == "spend":
        assert "causal allowance changed before commit" in rejection_reason
    else:
        assert "cannot reserve allowance from a terminal Contract" in rejection_reason
    assert not first_store.audit_allowance_checkpoints()
    assert not second_store.audit_allowance_checkpoints()
    assert not first_store.audit_publication_index()
    assert not second_store.audit_publication_index()
    assert (
        projection_context_module.load_candidate_projection_context
        is not original_loader
    )
    first_store.close()
    second_store.close()


def test_wide_pending_publication_head_uses_one_linear_relation_index():
    count = 64
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    sources = tuple(
        runtime.accept_contract(
            _identified(Contract(id="", name=f"wide-pending-source-{index}", budget=2))
        )
        for index in range(count)
    )
    children = tuple(
        _identified(
            Contract(
                id="",
                name=f"wide-pending-child-{index}",
                parent_id=source.id,
                budget=1,
            )
        )
        for index, source in enumerate(sources)
    )
    effects = tuple(contract_declaration_effect(child) for child in children)
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=effects,
        signer=runtime.signer,
        idempotency_key="wide-pending-relation-index",
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
    original_head = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.committed"
    )
    work = {"member_iteration": 0, "parent_iteration": 0, "contains": 0}

    class CountedTuple(tuple):
        def __new__(cls, values, key):
            instance = super().__new__(cls, values)
            instance.key = key
            return instance

        def __iter__(self):
            work[f"{self.key}_iteration"] += len(self)
            return super().__iter__()

        def __contains__(self, item):
            work["contains"] += len(self)
            return super().__contains__(item)

    head = replace(original_head)
    object.__setattr__(
        head,
        "payload",
        MappingProxyType(
            {
                **dict(original_head.payload),
                "committed_record_ids": CountedTuple(
                    original_head.payload["committed_record_ids"], "member"
                ),
            }
        ),
    )
    object.__setattr__(
        head, "causal_parents", CountedTuple(head.causal_parents, "parent")
    )
    pending = tuple(
        head if record.id == original_head.id else record
        for record in decision.runtime_records
    )

    batch = build_canonical_allowance_declaration_batch(
        pending,
        candidate.id,
        candidate_proposal=candidate,
        effect_fact_owners=decision.effect_fact_owners,
    )
    assert batch is not None
    assert publication_closure_for_funding_batch(batch) is not None
    member_parent_width = len(head.payload["committed_record_ids"]) + len(
        head.causal_parents
    )
    assert work["contains"] == 0
    assert (
        work["member_iteration"] + work["parent_iteration"] <= 8 * member_parent_width
    )
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
