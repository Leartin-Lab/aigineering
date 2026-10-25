"""Bounded read selection for pure built-in Contract declarations."""

from types import SimpleNamespace
from dataclasses import replace
import sqlite3

import pytest

from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.ids import contract_identity_v3
from aigineering.core.lifecycle_facts import commit_terminal_outcome
from aigineering.core.signing import create_verifier
from aigineering.core.actor_facts import candidate_actor_capabilities
from aigineering.core.effect_projection import project_effect_batch
from aigineering.core.injected_fact_replay import exact_single_effect_head_replay
from aigineering.core.projection_context import (
    ContractDeclarationProjectionRequest,
    ContractDeclarationProjectionInputs,
    build_contract_declaration_projection_request,
    load_contract_declaration_projection_context,
)
from aigineering.protocol.candidate import (
    CandidateClaimBinding,
    CandidateEffect,
    candidate_received_record,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract
from aigineering.protocol.wire import contract_to_dict
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.candidate import create_candidate_proposal
from conftest import candidate_runtime


def _candidate(*contracts, candidate_id="candidate:selection"):
    effects = tuple(
        CandidateEffect("contract.declare", {"contract": contract_to_dict(contract)})
        for contract in contracts
    )
    return SimpleNamespace(
        id=candidate_id,
        effects=effects,
        claim_binding=None,
    )


def test_request_selects_exact_existing_contract_asset_and_allowance_relations():
    parent = Contract(id="task:v6:parent", budget=10)
    child = Contract(
        id="task:v6:child",
        parent_id=parent.id,
        inputs=("used",),
        context_asset_ids=("asset:context",),
        sensitive_input_policy={
            "input_asset_bindings": {
                "used": ["asset:used"],
                "unused": ["asset:unused"],
            }
        },
    )

    request = build_contract_declaration_projection_request(
        _candidate(child, parent, child)
    )

    assert request is not None
    assert request.incoming_contract_ids == (child.id, parent.id)
    assert request.parent_contract_ids == (parent.id,)
    assert request.allowance_source_contract_ids == (parent.id,)
    assert request.asset_ids == ("asset:used", "asset:context")
    assert request.candidate_id == "candidate:selection"
    assert not request.include_candidate_replay


def test_unknown_effect_or_version_uses_no_narrow_request():
    contract = Contract(id="task:v7:future")
    unknown_version = build_contract_declaration_projection_request(
        _candidate(contract)
    )
    mixed = SimpleNamespace(
        id="candidate:mixed",
        claim_binding=None,
        effects=(
            CandidateEffect(
                "contract.declare", {"contract": contract_to_dict(contract)}
            ),
            CandidateEffect("asset.propose", {}),
        ),
    )

    assert unknown_version is None
    assert build_contract_declaration_projection_request(mixed) is None


def test_single_declaration_request_includes_replay_lookup():
    request = build_contract_declaration_projection_request(
        _candidate(Contract(id="task:v3:single"))
    )

    assert request is not None
    assert request.include_candidate_replay


def test_known_parser_error_falls_back_to_original_full_context():
    candidate = SimpleNamespace(
        id="candidate:bad",
        claim_binding=None,
        effects=(CandidateEffect("contract.declare", {"contract": "bad"}),),
    )

    class LegacyStore:
        def __init__(self):
            self.full_reads = []

        def get_all_contracts(self):
            self.full_reads.append("contracts")
            return [Contract(id="legacy")]

        def get_all_assets(self):
            self.full_reads.append("assets")
            return []

        def scan_runtime_records(self):
            self.full_reads.append("runtime")
            return [(1, create_runtime_record("unrelated.fact", {}))]

        def get_worker_registrations(self):
            self.full_reads.append("registrations")
            return []

        def get_claim_history_contract_ids(self):
            self.full_reads.append("claims")
            return frozenset()

    store = LegacyStore()
    context = load_contract_declaration_projection_context(store, candidate)

    assert context.contracts == (Contract(id="legacy"),)
    assert context.runtime_records[0].record_type == "unrelated.fact"
    assert set(store.full_reads) == {
        "contracts",
        "assets",
        "runtime",
        "registrations",
        "claims",
    }


def test_optional_reader_receives_immutable_request_and_returns_only_selection():
    contract = Contract(id="task:v6:child", parent_id="task:v6:parent")
    candidate = _candidate(contract)
    selected_record = create_runtime_record("allowance.reserved", {})

    class NarrowStore:
        def __init__(self):
            self.request = None

        def read_contract_declaration_projection_inputs(self, request):
            self.request = request
            return ContractDeclarationProjectionInputs(
                contracts=(Contract(id="task:v6:parent"),),
                assets=(),
                runtime_records=(selected_record,),
            )

        def get_all_contracts(self):
            raise AssertionError("narrow reader must not load every Contract")

        def get_all_assets(self):
            raise AssertionError("narrow reader must not load every Asset")

        def scan_runtime_records(self, **_kwargs):
            raise AssertionError("narrow reader must not scan runtime history")

    store = NarrowStore()
    context = load_contract_declaration_projection_context(store, candidate)

    assert context.contracts == (Contract(id="task:v6:parent"),)
    assert context.runtime_records == (selected_record,)
    assert store.request.parent_contract_ids == ("task:v6:parent",)
    with pytest.raises((AttributeError, TypeError)):
        store.request.candidate_id = "changed"


def test_unknown_preselection_programming_error_is_not_swallowed(monkeypatch):
    contract = Contract(id="task:v6:child", parent_id="task:v6:parent")
    candidate = _candidate(contract)

    def broken(_candidate):
        raise RuntimeError("programming failure")

    monkeypatch.setattr(
        "aigineering.core.projection_context.build_contract_declaration_projection_request",
        broken,
    )
    with pytest.raises(RuntimeError, match="programming failure"):
        load_contract_declaration_projection_context(object(), candidate)


def _closed_signed_history(store, publisher, count):
    contracts = tuple(
        build_control_plane_contract(
            name=f"bounded-history-{index}",
            inputs=(),
            outputs=(),
            activation="",
            budget=1,
        )
        for index in range(count)
    )
    if contracts:
        decision = publisher.publish(
            tuple(contract_declaration_effect(item) for item in contracts),
            idempotency_key=f"bounded-history-{count}",
        )
        assert decision.accepted
    for contract in contracts:
        assert commit_terminal_outcome(store, contract, "complete")
    return contracts


def _measure_missing_allowance_selection(store):
    count = 0

    def progress():
        nonlocal count
        count += 1
        return 0

    store._conn.set_progress_handler(progress, 1)
    selected = store.read_contract_declaration_projection_inputs(
        ContractDeclarationProjectionRequest(
            incoming_contract_ids=(),
            parent_contract_ids=(),
            allowance_source_contract_ids=("missing-source",),
            asset_ids=(),
            candidate_id="candidate:missing",
            include_candidate_replay=False,
        )
    )
    store._conn.set_progress_handler(None, 0)
    assert selected.runtime_records == ()
    return count


def _fixed_projection_candidate(store):
    from aigineering.core.ids import contract_from_fields
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.protocol.types import Asset

    assert isinstance(store, SQLiteStore)
    runtime = candidate_runtime(store)
    used = runtime.accept_asset(
        Asset(id="asset:fixed-used", name="dataset", content="used")
    )
    context = runtime.accept_asset(
        Asset(id="asset:fixed-context", name="context", content="context")
    )
    unused = runtime.accept_asset(
        Asset(id="asset:fixed-unused", name="unused", content="unused")
    )
    parent = runtime.accept_contract(
        build_control_plane_contract(
            name="fixed-parent", inputs=(), outputs=(), activation="", budget=16
        )
    )
    child = contract_from_fields(
        name="fixed-child",
        description="fixed projection measurement",
        inputs=("dataset",),
        outputs=(),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(context.name,),
        origin="system",
        parent_id=parent.id,
        worker_capabilities=(),
        worker_pools=(),
        minting_authority=(),
        sensitive_input_policy={
            "input_asset_bindings": {
                "dataset": [used.id],
                "unused": [unused.id],
            }
        },
        context_asset_ids=(context.id,),
    )
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(contract_declaration_effect(child),),
        signer=runtime.signer,
        idempotency_key="fixed-projection-measurement",
    )
    decision = CandidateCommitter(store, store).commit(candidate, runtime.genesis)
    assert decision.accepted, [
        record.payload.get("reason") for record in decision.runtime_records
    ]
    return runtime, parent, child, candidate, used, context, unused


def _observe_fixed_projection_selection(store, candidate):
    from collections import Counter
    import sqlite3

    request = build_contract_declaration_projection_request(candidate)
    assert request is not None
    assert request.include_candidate_replay
    measurements = Counter()
    original_row_factory = store._conn.row_factory
    original_get_contract = store.get_contract
    original_get_assets = store.get_assets_by_ids
    original_record_reader = store._row_to_runtime_record
    original_trace = store._conn.set_trace_callback

    def count_row(cursor, row):
        measurements["sqlite_rows_materialized"] += 1
        return sqlite3.Row(cursor, row)

    def count_get_contract(contract_id):
        measurements["get_contract_calls"] += 1
        return original_get_contract(contract_id)

    def count_get_assets(asset_ids):
        measurements["get_assets_by_ids_calls"] += 1
        return original_get_assets(asset_ids)

    def count_record(row):
        measurements["runtime_records_decoded"] += 1
        return original_record_reader(row)

    def count_select(statement):
        if statement.lstrip().upper().startswith("SELECT"):
            measurements["select_statements"] += 1

    store._conn.row_factory = count_row
    store.get_contract = count_get_contract
    store.get_assets_by_ids = count_get_assets
    store._row_to_runtime_record = count_record
    original_trace(count_select)
    try:
        selected = store.read_contract_declaration_projection_inputs(request)
    finally:
        original_trace(None)
        store._conn.row_factory = original_row_factory
        store.get_contract = original_get_contract
        store.get_assets_by_ids = original_get_assets
        store._row_to_runtime_record = original_record_reader

    types = Counter(record.record_type for record in selected.runtime_records)
    head = next(
        record
        for record in selected.runtime_records
        if record.record_type == "candidate.committed"
    )
    member_ids = set(head.payload["committed_record_ids"])
    selected_ids = {record.id for record in selected.runtime_records}
    measurements.update(
        {
            "contracts_returned": len(selected.contracts),
            "assets_returned": len(selected.assets),
            "runtime_records_returned": len(selected.runtime_records),
            "allowance_granted_returned": types["allowance.granted"],
            "allowance_reserved_returned": types["allowance.reserved"],
            "allowance_returned_returned": types["allowance.returned"],
            "allowance_extinguished_returned": types["allowance.extinguished"],
            "receipts_returned": types["candidate.received"],
            "heads_returned": types["candidate.committed"],
            "head_members_returned": len(member_ids & selected_ids),
            "head_members_expected": len(member_ids),
        }
    )
    return selected, dict(measurements)


def _relation_query_plan(store):
    clauses = (
        ("allowance.granted", "contract_id"),
        ("allowance.reserved", "source_contract_id"),
        ("allowance.returned", "source_contract_id"),
        ("allowance.returned", "from_contract_id"),
        ("allowance.extinguished", "contract_id"),
    )
    where = " OR ".join(
        "(record_type = ? AND json_extract(payload_json, '$.%s') IN (?))"
        % payload_field
        for _record_type, payload_field in clauses
    )
    values = tuple(
        value
        for record_type, _payload_field in clauses
        for value in (record_type, "task:fixed-parent")
    )
    return tuple(
        row[3]
        for row in store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM runtime_records WHERE " + where,
            values,
        )
    )


def test_closed_signed_history_does_not_expand_context_and_index_bounds_vm(
    tmp_path,
):
    from aigineering.core.sqlite_store import SQLiteStore

    stores = {}
    measurements_by_history = {}
    relation_counts_by_history = {}
    for history in (0, 16, 64):
        store = SQLiteStore(
            str(tmp_path / f"history-{history}.sqlite"),
            allowance_checkpoint_mode="reference",
        )
        runtime, parent, child, candidate, used, context, unused = (
            _fixed_projection_candidate(store)
        )
        _closed_signed_history(store, runtime.publisher, history)
        selected, measurements = _observe_fixed_projection_selection(store, candidate)
        assert {contract.id for contract in selected.contracts} == {
            parent.id,
            child.id,
        }
        assert {asset.id for asset in selected.assets} == {
            used.id,
            context.id,
        }
        assert unused.id not in {asset.id for asset in selected.assets}
        assert measurements["contracts_returned"] == 2
        assert measurements["assets_returned"] == 2
        assert measurements["allowance_granted_returned"] >= 1
        assert measurements["allowance_reserved_returned"] >= 1
        assert measurements["receipts_returned"] == 1
        assert measurements["heads_returned"] == 1
        assert (
            measurements["head_members_returned"]
            == measurements["head_members_expected"]
        )
        measurements_by_history[history] = measurements
        relation_types = (
            "allowance.granted",
            "allowance.reserved",
            "allowance.returned",
            "allowance.extinguished",
        )
        relation_counts_by_history[history] = tuple(
            (
                record_type,
                sum(
                    item.record_type == record_type for item in selected.runtime_records
                ),
            )
            for record_type in relation_types
        )
        baseline = _measure_missing_allowance_selection(store)
        if history == 64:
            stores[history] = store
            indexed_vm = baseline
            indexed_plan = _relation_query_plan(store)
            index_names = (
                "idx_runtime_records_allowance_granted_contract",
                "idx_runtime_records_allowance_reserved_source",
                "idx_runtime_records_allowance_returned_source",
                "idx_runtime_records_allowance_returned_from",
                "idx_runtime_records_allowance_extinguished_contract",
            )
            for name in index_names:
                store._conn.execute(f"DROP INDEX {name}")
            store._conn.commit()
            unindexed_vm = _measure_missing_allowance_selection(store)
            stores[history] = store
        else:
            store.close()

    store = stores[64]
    assert measurements_by_history[0] == measurements_by_history[16]
    assert measurements_by_history[16] == measurements_by_history[64]
    assert relation_counts_by_history[0] == relation_counts_by_history[16]
    assert relation_counts_by_history[16] == relation_counts_by_history[64]
    assert measurements_by_history[64]["sqlite_rows_materialized"] > 0
    assert measurements_by_history[64]["runtime_records_decoded"] > 0
    assert measurements_by_history[64]["select_statements"] > 0
    assert any(
        "USING INDEX idx_runtime_records_allowance_" in line for line in indexed_plan
    )
    assert unindexed_vm > 0
    assert indexed_vm <= unindexed_vm * 0.25
    history_ids = tuple(record.id for _, record in store.scan_runtime_records())
    db_path = tmp_path / "history-64.sqlite"
    store.close()
    reopened = SQLiteStore(str(db_path), allowance_checkpoint_mode="reference")
    index_names = {
        row[0]
        for row in reopened._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert {
        "idx_runtime_records_allowance_granted_contract",
        "idx_runtime_records_allowance_reserved_source",
        "idx_runtime_records_allowance_returned_source",
        "idx_runtime_records_allowance_returned_from",
        "idx_runtime_records_allowance_extinguished_contract",
        "idx_runtime_records_received_candidate",
    } <= index_names
    assert (
        tuple(record.id for _, record in reopened.scan_runtime_records()) == history_ids
    )
    reopened_vm = _measure_missing_allowance_selection(reopened)
    assert reopened_vm <= unindexed_vm * 0.25
    reopened.close()


def test_committer_uses_narrow_context_for_actual_builtin_effect_with_unused_injection(
    tmp_path, monkeypatch
):
    from aigineering.core.effect_projection import EffectHandler
    from aigineering.core.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "narrow-commit.sqlite"))
    runtime = candidate_runtime(store)
    contract = build_control_plane_contract(
        name="narrow-commit", inputs=(), outputs=(), activation="", budget=1
    )
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(contract_declaration_effect(contract),),
        signer=runtime.signer,
        idempotency_key="narrow-commit",
    )
    calls = []
    original = store.read_contract_declaration_projection_inputs

    def read_selected(request):
        calls.append(request)
        return original(request)

    monkeypatch.setattr(
        store, "read_contract_declaration_projection_inputs", read_selected
    )
    committer = CandidateCommitter(
        store,
        store,
        additional_effects={
            "custom.unused": EffectHandler("contract.publish", lambda *_args: None)
        },
    )

    decision = committer.commit(candidate, runtime.genesis)

    assert decision.accepted, [
        record.payload.get("reason") for record in decision.runtime_records
    ]
    assert len(calls) == 1
    assert calls[0].incoming_contract_ids == (contract.id,)
    selected = store.read_contract_declaration_projection_inputs(calls[0])
    head = next(
        record
        for _, record in store.scan_runtime_records(record_type="candidate.committed")
        if record.payload.get("candidate_id") == candidate.id
    )
    assert head in selected.runtime_records
    assert set(head.payload["committed_record_ids"]) <= {
        record.id for record in selected.runtime_records
    }

    receipt = next(
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if record.payload.get("candidate_id") == candidate.id
    )
    duplicate_receipt_payload = dict(receipt.payload)
    duplicate_receipt_payload["actor_id"] = "malformed-duplicate"
    duplicate_receipt = create_runtime_record(
        "candidate.received",
        duplicate_receipt_payload,
        causal_parents=receipt.causal_parents,
    )
    duplicate_head = create_runtime_record(
        "candidate.committed",
        {"candidate_id": candidate.id, "committed_record_ids": []},
        causal_parents=(receipt.id,),
    )
    with store._conn:
        store._insert_runtime_record(duplicate_receipt)
        store._insert_runtime_record(duplicate_head)
    duplicated = store.read_contract_declaration_projection_inputs(calls[0])
    assert (
        sum(
            record.record_type == "candidate.received"
            and record.payload.get("candidate_id") == candidate.id
            for record in duplicated.runtime_records
        )
        == 2
    )
    assert (
        sum(
            record.record_type == "candidate.committed"
            and record.payload.get("candidate_id") == candidate.id
            for record in duplicated.runtime_records
        )
        == 2
    )
    projected_records = tuple(
        record
        for record in decision.runtime_records
        if record.id in set(head.payload["committed_record_ids"])
    )
    assert exact_single_effect_head_replay(
        candidate, receipt.id, projected_records, selected.runtime_records
    )
    assert not exact_single_effect_head_replay(
        candidate, receipt.id, projected_records[:-1], selected.runtime_records
    )
    assert not exact_single_effect_head_replay(
        candidate,
        receipt.id,
        (*projected_records, create_runtime_record("custom.extra", {})),
        selected.runtime_records,
    )
    assert not exact_single_effect_head_replay(
        candidate,
        receipt.id,
        tuple(reversed(projected_records)),
        selected.runtime_records,
    )
    missing_member_context = tuple(
        record
        for record in selected.runtime_records
        if record.id != head.payload["committed_record_ids"][0]
    )
    assert not exact_single_effect_head_replay(
        candidate, receipt.id, projected_records, missing_member_context
    )
    reordered_member_ids = tuple(reversed(head.payload["committed_record_ids"]))
    reordered_head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate.id,
            "committed_record_ids": reordered_member_ids,
        },
        causal_parents=(receipt.id, *reordered_member_ids),
    )
    reordered_head_context = tuple(
        record for record in selected.runtime_records if record.id != head.id
    ) + (reordered_head,)
    assert not exact_single_effect_head_replay(
        candidate, receipt.id, projected_records, reordered_head_context
    )
    duplicated_member_ids = (
        *head.payload["committed_record_ids"],
        head.payload["committed_record_ids"][0],
    )
    duplicated_member_head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate.id,
            "committed_record_ids": duplicated_member_ids,
        },
        causal_parents=(receipt.id, *duplicated_member_ids),
    )
    duplicated_member_context = tuple(
        record for record in selected.runtime_records if record.id != head.id
    ) + (duplicated_member_head,)
    assert not exact_single_effect_head_replay(
        candidate, receipt.id, projected_records, duplicated_member_context
    )
    assert not exact_single_effect_head_replay(
        candidate, receipt.id, projected_records, duplicated.runtime_records
    )
    store.close()


def test_custom_store_without_optional_reader_uses_full_context_at_commit(
    tmp_path, monkeypatch
):
    from aigineering.core import projection_context
    from aigineering.core.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "legacy-custom-store.sqlite"))
    runtime = candidate_runtime(store)
    contract = build_control_plane_contract(
        name="legacy-custom-store", inputs=(), outputs=(), activation="", budget=1
    )
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(contract_declaration_effect(contract),),
        signer=runtime.signer,
        idempotency_key="legacy-custom-store",
    )

    class LegacyStore:
        def __init__(self, delegate):
            self.delegate = delegate
            self.full_read_calls = []

        def __getattr__(self, name):
            if name == "read_contract_declaration_projection_inputs":
                raise AttributeError(name)
            if name in {"get_all_contracts", "get_all_assets", "scan_runtime_records"}:
                self.full_read_calls.append(name)
            return getattr(self.delegate, name)

    legacy = LegacyStore(store)
    full_loads = []
    original_full_loader = projection_context.load_effect_projection_context

    def observe_full(*args, **kwargs):
        full_loads.append(True)
        return original_full_loader(*args, **kwargs)

    monkeypatch.setattr(
        projection_context, "load_effect_projection_context", observe_full
    )
    assert not hasattr(legacy, "read_contract_declaration_projection_inputs")

    decision = CandidateCommitter(legacy, legacy).commit(candidate, runtime.genesis)

    assert decision.accepted
    assert full_loads
    assert {"get_all_contracts", "get_all_assets", "scan_runtime_records"} <= set(
        legacy.full_read_calls
    )
    store.close()


def test_mixed_candidate_and_executed_injected_handler_use_full_context(
    tmp_path, monkeypatch
):
    from aigineering.core import projection_context
    from aigineering.core.effect_projection import EffectHandler, EffectProjection
    from aigineering.core.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "mixed-full-context.sqlite"))
    runtime = candidate_runtime(store)
    contract = build_control_plane_contract(
        name="mixed-full-context", inputs=(), outputs=(), activation="", budget=1
    )
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(
            contract_declaration_effect(contract),
            CandidateEffect("custom.noop", {}),
        ),
        signer=runtime.signer,
        idempotency_key="mixed-full-context",
    )
    reader_calls = []
    original_reader = store.read_contract_declaration_projection_inputs

    def observe_reader(request):
        reader_calls.append(request)
        return original_reader(request)

    monkeypatch.setattr(
        store, "read_contract_declaration_projection_inputs", observe_reader
    )
    full_loads = []
    original_full_loader = projection_context.load_effect_projection_context

    def observe_full(*args, **kwargs):
        full_loads.append(True)
        return original_full_loader(*args, **kwargs)

    monkeypatch.setattr(
        projection_context, "load_effect_projection_context", observe_full
    )
    injected_calls = []

    def inject(effect, _candidate, _receipt_id, _context):
        injected_calls.append(effect.effect_type)
        return EffectProjection((), relation_target=None)

    decision = CandidateCommitter(
        store,
        store,
        additional_effects={"custom.noop": EffectHandler("contract.publish", inject)},
    ).commit(candidate, runtime.genesis)

    assert decision.accepted, [
        record.payload.get("reason") for record in decision.runtime_records
    ]
    assert not reader_calls
    assert full_loads
    assert injected_calls
    assert set(injected_calls) == {"custom.noop"}
    store.close()


def test_same_batch_reverse_parent_child_projection_matches_full_context(tmp_path):
    from aigineering.core.projection_context import (
        load_effect_projection_context,
    )
    from aigineering.core.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "same-batch-differential.sqlite"))
    runtime = candidate_runtime(store)
    parent = build_control_plane_contract(
        name="same-batch-parent", inputs=(), outputs=(), activation="", budget=5
    )
    child = Contract(
        id="",
        parent_id=parent.id,
        name="same-batch-child",
        budget=2,
    )
    child = replace(child, id=contract_identity_v3(child))
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(
            contract_declaration_effect(child),
            contract_declaration_effect(parent),
        ),
        signer=runtime.signer,
        idempotency_key="same-batch-reverse",
    )
    narrow = load_contract_declaration_projection_context(
        store, candidate, actor_keys=runtime.genesis.root_keys
    )
    full = load_effect_projection_context(store, actor_keys=runtime.genesis.root_keys)

    receipt = candidate_received_record(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=runtime.genesis.root_keys,
    )
    capabilities = candidate_actor_capabilities(candidate, runtime.genesis.root_keys)
    narrow_projection = project_effect_batch(
        candidate,
        receipt.id,
        capabilities,
        narrow,
    )
    full_projection = project_effect_batch(
        candidate,
        receipt.id,
        capabilities,
        full,
    )

    assert narrow.contracts == ()
    assert narrow_projection.records
    assert tuple(
        (item.id, item.record_type, item.payload, item.causal_parents)
        for item in narrow_projection.records
    ) == tuple(
        (item.id, item.record_type, item.payload, item.causal_parents)
        for item in full_projection.records
    )
    assert narrow_projection.contracts == full_projection.contracts
    assert narrow_projection.projected_effects == full_projection.projected_effects
    assert len(narrow_projection.contracts) == 2
    store.close()


def test_sibling_declarations_match_full_context_and_commit_through_reader(
    tmp_path, monkeypatch
):
    from aigineering.core.projection_context import load_effect_projection_context
    from aigineering.core.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "sibling-differential.sqlite"))
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(
        build_control_plane_contract(
            name="sibling-parent", inputs=(), outputs=(), activation="", budget=8
        )
    )
    children = tuple(
        replace(
            Contract(id="", parent_id=parent.id, name=f"sibling-{index}", budget=1),
            id=contract_identity_v3(
                Contract(id="", parent_id=parent.id, name=f"sibling-{index}", budget=1)
            ),
        )
        for index in range(2)
    )
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=tuple(contract_declaration_effect(child) for child in children),
        signer=runtime.signer,
        idempotency_key="sibling-differential",
    )
    reader_calls = []
    original_reader = store.read_contract_declaration_projection_inputs

    def observe_reader(request):
        reader_calls.append(request)
        return original_reader(request)

    monkeypatch.setattr(
        store, "read_contract_declaration_projection_inputs", observe_reader
    )
    narrow = load_contract_declaration_projection_context(
        store, candidate, actor_keys=runtime.genesis.root_keys
    )
    full = load_effect_projection_context(store, actor_keys=runtime.genesis.root_keys)
    receipt = candidate_received_record(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=runtime.genesis.root_keys,
    )
    capabilities = candidate_actor_capabilities(candidate, runtime.genesis.root_keys)
    narrow_projection = project_effect_batch(
        candidate, receipt.id, capabilities, narrow
    )
    full_projection = project_effect_batch(candidate, receipt.id, capabilities, full)

    assert len(reader_calls) == 1
    assert reader_calls[0].parent_contract_ids == (parent.id,)
    assert tuple(
        (item.id, item.record_type, item.payload, item.causal_parents)
        for item in narrow_projection.records
    ) == tuple(
        (item.id, item.record_type, item.payload, item.causal_parents)
        for item in full_projection.records
    )
    decision = CandidateCommitter(store, store).commit(candidate, runtime.genesis)
    assert decision.accepted
    assert len(reader_calls) == 2
    store.close()


@pytest.mark.parametrize("version", (3, 4, 5, 6))
def test_versioned_input_and_context_selection_matches_full_projection(
    tmp_path, monkeypatch, version
):
    from aigineering.core.ids import contract_from_fields
    from aigineering.core.projection_context import load_effect_projection_context
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.protocol.types import Asset

    store = SQLiteStore(str(tmp_path / f"versioned-projection-v{version}.sqlite"))
    runtime = candidate_runtime(store)
    used = runtime.accept_asset(
        Asset(id=f"asset:v{version}-used", name="dataset", content="used")
    )
    context = runtime.accept_asset(
        Asset(id=f"asset:v{version}-context", name="context", content="context")
    )
    unused = runtime.accept_asset(
        Asset(id=f"asset:v{version}-unused", name="unused", content="unused")
    )
    parent = runtime.accept_contract(
        build_control_plane_contract(
            name=f"versioned-parent-v{version}",
            inputs=(),
            outputs=(),
            activation="",
            budget=8,
        )
    )
    kwargs = {
        "name": f"versioned-child-v{version}",
        "description": "versioned projection differential",
        "inputs": ("dataset",),
        "outputs": (),
        "activation": "",
        "budget": 2,
        "tool_scope": (),
        "labels": (context.name,) if version >= 4 else (),
        "origin": "system",
        "parent_id": parent.id,
        "worker_capabilities": (),
        "worker_pools": (),
        "minting_authority": (),
        "sensitive_input_policy": {"dataset": {"disclose": True}},
        "context_asset_ids": (context.id,) if version >= 4 else (),
    }
    if version >= 5:
        kwargs["delegation_capabilities"] = ("delegate",)
    if version == 6:
        kwargs["sensitive_input_policy"] = {
            "input_asset_bindings": {
                "dataset": [used.id],
                "unused": [unused.id],
            }
        }
    child = contract_from_fields(**kwargs)
    assert child.id.startswith(f"task:v{version}:")
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(contract_declaration_effect(child),),
        signer=runtime.signer,
        idempotency_key=f"versioned-child-v{version}",
    )
    reader_calls = []
    original_reader = store.read_contract_declaration_projection_inputs

    def observe_reader(request):
        reader_calls.append(request)
        return original_reader(request)

    monkeypatch.setattr(
        store, "read_contract_declaration_projection_inputs", observe_reader
    )
    narrow = load_contract_declaration_projection_context(
        store, candidate, actor_keys=runtime.genesis.root_keys
    )
    full = load_effect_projection_context(store, actor_keys=runtime.genesis.root_keys)
    assert len(reader_calls) == 1
    if version < 4:
        assert narrow.assets == ()
    elif version < 6:
        assert {asset.id for asset in narrow.assets} == {context.id}
    else:
        assert {asset.id for asset in narrow.assets} == {context.id, used.id}
        assert unused.id not in {asset.id for asset in narrow.assets}

    receipt = candidate_received_record(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=runtime.genesis.root_keys,
    )
    capabilities = candidate_actor_capabilities(candidate, runtime.genesis.root_keys)
    narrow_projection = project_effect_batch(
        candidate, receipt.id, capabilities, narrow
    )
    full_projection = project_effect_batch(candidate, receipt.id, capabilities, full)
    assert tuple(
        (item.id, item.record_type, item.payload, item.causal_parents)
        for item in narrow_projection.records
    ) == tuple(
        (item.id, item.record_type, item.payload, item.causal_parents)
        for item in full_projection.records
    )
    assert narrow_projection.contracts == full_projection.contracts
    assert narrow_projection.projected_effects == full_projection.projected_effects
    store.close()


@pytest.mark.parametrize(
    "case", ("missing", "wrong_name", "tombstoned", "parent_bound")
)
def test_bound_asset_and_parent_rejections_match_full_context(
    tmp_path, monkeypatch, case
):
    from aigineering.core.ids import contract_from_fields
    from aigineering.core.projection_context import load_effect_projection_context
    from aigineering.core.provenance import sign_asset
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.protocol.types import Asset

    store = SQLiteStore(str(tmp_path / f"rejection-{case}.sqlite"))
    runtime = candidate_runtime(store)
    expected_id = "asset:missing-binding"
    if case != "missing":
        asset_name = "other" if case == "wrong_name" else "dataset"
        asset = sign_asset(
            Asset(
                id=f"asset:{case}",
                name=asset_name,
                content="bound source",
                tombstoned=case == "tombstoned",
            ),
            "projection-fixture",
        )
        store.add_asset(asset)
        expected_id = asset.id
    parent = runtime.accept_contract(
        build_control_plane_contract(
            name=f"rejection-parent-{case}",
            inputs=(),
            outputs=(),
            activation="",
            budget=1 if case == "parent_bound" else 8,
        )
    )
    child = contract_from_fields(
        name=f"rejection-child-{case}",
        description="full-context rejection differential",
        inputs=("dataset",),
        outputs=(),
        activation="",
        budget=2 if case == "parent_bound" else 1,
        tool_scope=(),
        labels=(),
        origin="system",
        parent_id=parent.id,
        worker_capabilities=(),
        worker_pools=(),
        minting_authority=(),
        sensitive_input_policy={"input_asset_bindings": {"dataset": [expected_id]}},
    )
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(contract_declaration_effect(child),),
        signer=runtime.signer,
        idempotency_key=f"rejection-child-{case}",
    )
    reader_calls = []
    original_reader = store.read_contract_declaration_projection_inputs

    def observe_reader(request):
        reader_calls.append(request)
        return original_reader(request)

    monkeypatch.setattr(
        store, "read_contract_declaration_projection_inputs", observe_reader
    )
    narrow = load_contract_declaration_projection_context(
        store, candidate, actor_keys=runtime.genesis.root_keys
    )
    full = load_effect_projection_context(store, actor_keys=runtime.genesis.root_keys)
    assert len(reader_calls) == 1
    receipt = candidate_received_record(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=runtime.genesis.root_keys,
    )
    capabilities = candidate_actor_capabilities(candidate, runtime.genesis.root_keys)

    def result(context):
        try:
            projection = project_effect_batch(
                candidate, receipt.id, capabilities, context
            )
        except (TypeError, ValueError) as exc:
            return type(exc), str(exc)
        return tuple(
            (item.id, item.record_type, item.payload, item.causal_parents)
            for item in projection.records
        )

    narrow_result = result(narrow)
    assert narrow_result == result(full)
    assert isinstance(narrow_result[0], type)
    assert issubclass(narrow_result[0], (TypeError, ValueError))
    store.close()


def test_forged_malformed_candidate_keeps_authentication_rejection_and_claim_state(
    tmp_path, monkeypatch
):
    from aigineering.core import projection_context
    from aigineering.core.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "forged-fallback.sqlite"))
    runtime = candidate_runtime(store)
    claimed_contract = build_control_plane_contract(
        name="forged-claim-target", inputs=(), outputs=(), activation="", budget=1
    )
    runtime.accept_contract(claimed_contract)
    claim_binding = CandidateClaimBinding(
        claimed_contract.id, "claim:forged-target", 1, "pkg:forged-target"
    )
    store.persist_claim(
        claim_binding.claim_id,
        claimed_contract.id,
        runtime.actor_key.actor_id,
        "2099-01-01T00:00:00+00:00",
        package_id=claim_binding.package_id,
        epoch=claim_binding.claim_epoch,
    )
    claim_before = store.get_claim(claimed_contract.id)
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(CandidateEffect("contract.declare", {"contract": []}),),
        signer=runtime.signer,
        idempotency_key="forged-malformed",
        claim_binding=claim_binding,
    )
    forged = replace(candidate, signature="forged-signature")
    full_loads = []
    original_loader = projection_context.load_effect_projection_context

    def load_full(*args, **kwargs):
        full_loads.append(True)
        return original_loader(*args, **kwargs)

    monkeypatch.setattr(projection_context, "load_effect_projection_context", load_full)
    decision = CandidateCommitter(store, store).commit(forged, runtime.genesis)

    assert not decision.accepted
    assert full_loads
    records = [record for _, record in store.scan_runtime_records()]
    rejections = [
        record
        for record in records
        if record.record_type == "candidate.authentication_rejected"
        and record.payload.get("claimed_candidate_id") == candidate.id
    ]
    assert len(rejections) == 1
    assert not any(
        record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate.id
        for record in records
    )
    assert store.get_claim(claimed_contract.id) == claim_before
    store.close()


def test_signed_malformed_domain_candidate_keeps_reducer_rejection_and_claim_state(
    tmp_path, monkeypatch
):
    from aigineering.core import projection_context
    from aigineering.core.commitment import reduce_candidate
    from aigineering.core.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "signed-malformed-fallback.sqlite"))
    runtime = candidate_runtime(store)
    from aigineering.core.worker_routing import WorkerRegistration

    runtime.register_worker(
        WorkerRegistration(
            worker_id=runtime.actor_key.actor_id,
            actor_id=runtime.actor_key.actor_id,
            key_id=runtime.actor_key.key_id,
        )
    )
    claimed_contract = runtime.accept_contract(
        build_control_plane_contract(
            name="signed-malformed-claim-target",
            inputs=(),
            outputs=(),
            activation="",
            budget=1,
        )
    )
    claim_binding = CandidateClaimBinding(
        claimed_contract.id,
        "claim:signed-malformed",
        1,
        "pkg:signed-malformed",
    )
    store.persist_claim(
        claim_binding.claim_id,
        claimed_contract.id,
        runtime.actor_key.actor_id,
        "2099-01-01T00:00:00+00:00",
        package_id=claim_binding.package_id,
        epoch=claim_binding.claim_epoch,
    )
    claim_before = store.get_claim(claimed_contract.id)
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(CandidateEffect("contract.declare", {"contract": "malformed"}),),
        signer=runtime.signer,
        idempotency_key="signed-malformed-domain",
        claim_binding=claim_binding,
    )
    full_loads = []
    original_full_loader = projection_context.load_effect_projection_context

    def observe_full(*args, **kwargs):
        full_loads.append(True)
        return original_full_loader(*args, **kwargs)

    monkeypatch.setattr(
        projection_context, "load_effect_projection_context", observe_full
    )
    fallback_context = load_contract_declaration_projection_context(
        store, candidate, actor_keys=runtime.genesis.root_keys
    )
    full_context = original_full_loader(store, actor_keys=runtime.genesis.root_keys)
    fallback_decision = reduce_candidate(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=runtime.genesis.root_keys,
        projection_context=fallback_context,
    )
    full_decision = reduce_candidate(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=runtime.genesis.root_keys,
        projection_context=full_context,
    )
    assert fallback_decision.accepted == full_decision.accepted
    assert tuple(
        (record.id, record.record_type, record.payload, record.causal_parents)
        for record in fallback_decision.runtime_records
    ) == tuple(
        (record.id, record.record_type, record.payload, record.causal_parents)
        for record in full_decision.runtime_records
    )
    decision = CandidateCommitter(store, store).commit(candidate, runtime.genesis)

    assert not decision.accepted
    assert full_loads
    records = [record for _, record in store.scan_runtime_records()]
    assert any(
        record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate.id
        for record in records
    )
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == candidate.id
        for record in records
    )
    assert not any(
        record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == candidate.id
        for record in records
    )
    claim_after = store.get_claim(claimed_contract.id)
    assert claim_after["claim_id"] == claim_before["claim_id"]
    assert claim_after["status"] == "submitted"
    store.close()


def test_reader_returns_exact_source_allowance_facts_in_revision_order(tmp_path):
    from aigineering.core.sqlite_store import SQLiteStore

    store = SQLiteStore(
        str(tmp_path / "source-relations.sqlite"),
        allowance_checkpoint_mode="reference",
    )
    runtime = candidate_runtime(store)
    parent = build_control_plane_contract(
        name="allowance-parent", inputs=(), outputs=(), activation="", budget=5
    )
    runtime.accept_contract(parent)
    child = Contract(
        id="",
        parent_id=parent.id,
        name="allowance-child",
        budget=2,
    )
    child = replace(child, id=contract_identity_v3(child))
    runtime.accept_contract(child)
    request = ContractDeclarationProjectionRequest(
        incoming_contract_ids=(child.id,),
        parent_contract_ids=(parent.id,),
        allowance_source_contract_ids=(parent.id,),
        asset_ids=(),
        candidate_id="candidate:unrelated",
        include_candidate_replay=False,
    )

    selected = store.read_contract_declaration_projection_inputs(request)
    all_records = store.scan_runtime_records()
    expected = tuple(
        record
        for _, record in all_records
        if (
            record.record_type == "allowance.granted"
            and record.payload.get("contract_id") == parent.id
        )
        or (
            record.record_type == "allowance.reserved"
            and record.payload.get("source_contract_id") == parent.id
        )
        or (
            record.record_type == "allowance.returned"
            and (
                record.payload.get("source_contract_id") == parent.id
                or record.payload.get("from_contract_id") == parent.id
            )
        )
        or (
            record.record_type == "allowance.extinguished"
            and record.payload.get("contract_id") == parent.id
        )
    )

    assert selected.contracts == (child, parent)
    assert selected.runtime_records == expected
    assert [item.record_type for item in expected] == [
        "allowance.granted",
        "allowance.reserved",
    ]
    store.close()


def test_reader_chunks_relation_query_below_sqlite_variable_limit(tmp_path):
    from aigineering.core.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "relation-variable-limit.sqlite"))
    store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    request = ContractDeclarationProjectionRequest(
        incoming_contract_ids=(),
        parent_contract_ids=(),
        allowance_source_contract_ids=tuple(
            f"task:parent:{index}" for index in range(199)
        ),
        asset_ids=(),
        candidate_id="candidate:no-replay",
        include_candidate_replay=False,
    )

    selected = store.read_contract_declaration_projection_inputs(request)

    assert selected.runtime_records == ()
    store.close()


@pytest.mark.parametrize("variable_limit", (0, 1, 5))
def test_reader_refuses_relation_query_when_fixed_parameters_exceed_limit(
    tmp_path, variable_limit
):
    from aigineering.core.sqlite_store import SQLiteStore

    store = SQLiteStore(
        str(tmp_path / f"relation-limit-{variable_limit}.sqlite"),
        allowance_checkpoint_mode="reference",
    )
    store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, variable_limit)
    request = ContractDeclarationProjectionRequest(
        incoming_contract_ids=(),
        parent_contract_ids=(),
        allowance_source_contract_ids=("task:parent",),
        asset_ids=(),
        candidate_id="candidate:no-replay",
        include_candidate_replay=False,
    )

    with pytest.raises(sqlite3.OperationalError, match="variable limit"):
        store.read_contract_declaration_projection_inputs(request)
    store.close()


def test_actual_declaration_reader_selects_many_distinct_parent_relations(
    tmp_path, monkeypatch
):
    from aigineering.core.ids import contract_from_fields
    from aigineering.core.sqlite_store import SQLiteStore

    store = SQLiteStore(
        str(tmp_path / "many-parent-relations.sqlite"),
        allowance_checkpoint_mode="reference",
    )
    runtime = candidate_runtime(store)
    parents = tuple(
        runtime.accept_contract(
            build_control_plane_contract(
                name=f"multi-parent-{index}",
                inputs=(),
                outputs=(),
                activation="",
                budget=4,
            )
        )
        for index in range(4)
    )
    children = tuple(
        contract_from_fields(
            name=f"multi-parent-child-{index}",
            description="distinct parent selection",
            inputs=(),
            outputs=(),
            activation="",
            budget=1,
            tool_scope=(),
            labels=(),
            origin="system",
            parent_id=parent.id,
            worker_capabilities=(),
            worker_pools=(),
            minting_authority=(),
        )
        for index, parent in enumerate(parents)
    )
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=tuple(contract_declaration_effect(child) for child in children),
        signer=runtime.signer,
        idempotency_key="multi-parent-declarations",
    )
    reader_calls = []
    original_reader = store.read_contract_declaration_projection_inputs

    def observe_reader(request):
        reader_calls.append(request)
        return original_reader(request)

    monkeypatch.setattr(
        store, "read_contract_declaration_projection_inputs", observe_reader
    )
    decision = CandidateCommitter(store, store).commit(candidate, runtime.genesis)

    assert decision.accepted
    assert len(reader_calls) == 1
    assert reader_calls[0].parent_contract_ids == tuple(parent.id for parent in parents)
    selected = original_reader(reader_calls[0])
    assert {contract.id for contract in selected.contracts} == {
        *(parent.id for parent in parents),
        *(child.id for child in children),
    }
    reservations = [
        record
        for record in selected.runtime_records
        if record.record_type == "allowance.reserved"
    ]
    assert {record.payload.get("source_contract_id") for record in reservations} == {
        parent.id for parent in parents
    }
    store.close()


def test_memory_store_implements_same_optional_projection_reader():
    from aigineering.core.store import MemoryStore

    store = MemoryStore()
    runtime = candidate_runtime(store)
    contract = build_control_plane_contract(
        name="memory-narrow", inputs=(), outputs=(), activation="", budget=1
    )
    decision = runtime.publisher.publish(
        (contract_declaration_effect(contract),), idempotency_key="memory-narrow"
    )

    assert decision.accepted
    request = ContractDeclarationProjectionRequest(
        incoming_contract_ids=(contract.id,),
        parent_contract_ids=(),
        allowance_source_contract_ids=(),
        asset_ids=(),
        candidate_id="candidate:missing",
        include_candidate_replay=False,
    )
    selected = store.read_contract_declaration_projection_inputs(request)

    assert selected.contracts == (contract,)
    assert selected.runtime_records == ()


def test_memory_reader_ignores_malformed_unrelated_allowance_shape():
    from aigineering.core.projection_context import load_effect_projection_context
    from aigineering.core.store import MemoryStore
    from aigineering.protocol.candidate import candidate_received_record

    store = MemoryStore()
    runtime = candidate_runtime(store)
    parent = runtime.accept_contract(
        build_control_plane_contract(
            name="memory-malformed-source",
            inputs=(),
            outputs=(),
            activation="",
            budget=4,
        )
    )
    malformed = create_runtime_record(
        "allowance.granted", {"contract_id": {"not": "a source ID"}}
    )
    store._runtime_records[malformed.id] = (
        store.get_runtime_revision() + 1,
        malformed,
    )
    child = Contract(id="", parent_id=parent.id, name="memory-child", budget=1)
    child = replace(child, id=contract_identity_v3(child))
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=(contract_declaration_effect(child),),
        signer=runtime.signer,
        idempotency_key="memory-malformed-source",
    )
    narrow = load_contract_declaration_projection_context(
        store, candidate, actor_keys=runtime.genesis.root_keys
    )
    full = load_effect_projection_context(store, actor_keys=runtime.genesis.root_keys)
    receipt = candidate_received_record(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=runtime.genesis.root_keys,
    )
    capabilities = candidate_actor_capabilities(candidate, runtime.genesis.root_keys)

    narrow_projection = project_effect_batch(
        candidate, receipt.id, capabilities, narrow
    )
    full_projection = project_effect_batch(candidate, receipt.id, capabilities, full)

    assert malformed in full.runtime_records
    assert malformed not in narrow.runtime_records
    assert tuple(
        (item.id, item.record_type, item.payload, item.causal_parents)
        for item in narrow_projection.records
    ) == tuple(
        (item.id, item.record_type, item.payload, item.causal_parents)
        for item in full_projection.records
    )
