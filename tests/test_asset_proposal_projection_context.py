"""Narrow immutable context reads for built-in Asset proposals."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aigineering.core.commitment import CandidateCommitter, reduce_candidate
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.ids import contract_identity_v3
from aigineering.core.injected_fact_replay import exact_single_effect_head_replay
from aigineering.core.lifecycle_facts import commit_terminal_outcome
from aigineering.core.projection_context import (
    AssetProposalProjectionInputs,
    AssetProposalProjectionRequest,
    EffectProjectionContext,
    build_asset_proposal_projection_request,
    load_asset_proposal_projection_context,
    load_candidate_projection_context,
    load_effect_projection_context,
)
from aigineering.core.signing import create_verifier
from aigineering.protocol.candidate import (
    CandidateClaimBinding,
    CandidateEffect,
    create_candidate_proposal,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset, Contract
from conftest import candidate_runtime
from conftest import hosted_worker


def _candidate(runtime, effects, *, key="asset-projection", claim_binding=None):
    return create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        effects=tuple(effects),
        signer=runtime.signer,
        idempotency_key=key,
        claim_binding=claim_binding,
    )


def _asset_effect(name="result", content="value", *, created_by=""):
    return asset_proposal_effect(
        Asset(id="", name=name, content=content, created_by=created_by)
    )


def _reduction_semantics(decision):
    return (
        decision.accepted,
        decision.contracts,
        decision.assets,
        tuple(
            (record.id, record.record_type, record.payload, record.causal_parents)
            for record in decision.runtime_records
        ),
        decision.trace_entries,
    )


def _measure_reader(store, request):
    statements: list[list[object]] = []
    vm_steps = 0

    def trace(sql: str) -> None:
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append([sql, 0])

    def progress() -> int:
        nonlocal vm_steps
        vm_steps += 1
        if statements:
            statements[-1][1] = int(statements[-1][1]) + 1
        return 0

    store._conn.set_trace_callback(trace)
    store._conn.set_progress_handler(progress, 1)
    try:
        selected = store.read_asset_proposal_projection_inputs(request)
    finally:
        store._conn.set_trace_callback(None)
        store._conn.set_progress_handler(None, 0)
    return selected, [(str(sql), int(steps)) for sql, steps in statements], vm_steps


def _closed_history(store, runtime, count):
    contracts = tuple(
        build_control_plane_contract(
            name=f"asset-projection-closed-{index}",
            inputs=(),
            outputs=(),
            activation="",
            budget=1,
        )
        for index in range(count)
    )
    if contracts:
        decision = runtime.publisher.publish(
            tuple(contract_declaration_effect(contract) for contract in contracts),
            idempotency_key=f"asset-projection-history-{count}",
        )
        assert decision.accepted
    for contract in contracts:
        assert commit_terminal_outcome(store, contract, "complete")


def test_builder_freezes_only_exact_homogeneous_builtin_asset_proposals():
    from aigineering.core.sqlite_store import SQLiteStore

    with SQLiteStore(":memory:") as store:
        runtime = candidate_runtime(store)
        one = _candidate(runtime, (_asset_effect(),), key="one")
        many = _candidate(
            runtime,
            (_asset_effect("one"), _asset_effect("two")),
            key="many",
        )
        request = build_asset_proposal_projection_request(one)
        assert request == AssetProposalProjectionRequest(
            candidate_id=one.id,
            claim_bound_contract_id=None,
            include_candidate_replay=True,
        )
        assert build_asset_proposal_projection_request(many) == replace(
            request, candidate_id=many.id, include_candidate_replay=False
        )

        mixed = replace(
            one,
            effects=(
                one.effects[0],
                CandidateEffect("contract.declare", {"contract": {}}),
            ),
        )
        assert build_asset_proposal_projection_request(mixed) is None

        empty_effects = replace(one)
        object.__setattr__(empty_effects, "effects", ())
        assert build_asset_proposal_projection_request(empty_effects) is None
        assert (
            build_asset_proposal_projection_request(
                SimpleNamespace(id=one.id, effects=one.effects, claim_binding=None)
            )
            is None
        )

        wrong_effect = replace(one)
        bad_type = replace(one.effects[0])
        object.__setattr__(bad_type, "effect_type", 7)
        object.__setattr__(wrong_effect, "effects", (bad_type,))
        assert build_asset_proposal_projection_request(wrong_effect) is None

        wrong_effect_instance = replace(one)
        object.__setattr__(
            wrong_effect_instance,
            "effects",
            (SimpleNamespace(effect_type="asset.propose"),),
        )
        assert build_asset_proposal_projection_request(wrong_effect_instance) is None

        wrong_binding = replace(one)
        object.__setattr__(
            wrong_binding,
            "claim_binding",
            SimpleNamespace(contract_id="task:v3:parent"),
        )
        assert build_asset_proposal_projection_request(wrong_binding) is None


def test_candidate_context_facade_is_called_once_and_builds_asset_hint_once(
    temp_sqlite_store, monkeypatch
):
    from aigineering.core import commitment, projection_context

    runtime = candidate_runtime(temp_sqlite_store)
    candidate = _candidate(runtime, (_asset_effect(),), key="facade-asset")
    original_builder = projection_context.build_asset_proposal_projection_request
    builder_calls = []

    def build_once(value):
        builder_calls.append(value.id)
        return original_builder(value)

    monkeypatch.setattr(
        projection_context, "build_asset_proposal_projection_request", build_once
    )
    original_facade = commitment.context_inputs.load_candidate_projection_context
    facade_calls = []

    def observe_facade(*args, **kwargs):
        facade_calls.append(args[1].id)
        return original_facade(*args, **kwargs)

    monkeypatch.setattr(
        commitment.context_inputs, "load_candidate_projection_context", observe_facade
    )
    original_reader = temp_sqlite_store.read_asset_proposal_projection_inputs
    reader_calls = []

    def read_once(request):
        reader_calls.append(request)
        return original_reader(request)

    monkeypatch.setattr(
        temp_sqlite_store, "read_asset_proposal_projection_inputs", read_once
    )
    decision = CandidateCommitter(temp_sqlite_store, temp_sqlite_store).commit(
        candidate, runtime.genesis
    )
    assert decision.accepted
    assert facade_calls == [candidate.id]
    assert builder_calls == [candidate.id]
    assert len(reader_calls) == 1
    assert reader_calls[0].candidate_id == candidate.id


def test_candidate_context_facade_routes_declaration_and_mixed_candidates(
    temp_sqlite_store, monkeypatch
):
    from aigineering.core import projection_context

    runtime = candidate_runtime(temp_sqlite_store)
    contract = build_control_plane_contract(
        name="facade-declaration", inputs=(), outputs=(), activation="", budget=1
    )
    declaration = _candidate(
        runtime,
        (contract_declaration_effect(contract),),
        key="facade-declaration",
    )
    mixed = _candidate(
        runtime,
        (_asset_effect("facade-mixed"), contract_declaration_effect(contract)),
        key="facade-mixed",
    )
    old_declaration = projection_context.load_contract_declaration_projection_context
    declaration_calls = []

    def observe_declaration(store, candidate, **kwargs):
        declaration_calls.append(candidate.id)
        return old_declaration(store, candidate, **kwargs)

    old_full = projection_context.load_effect_projection_context
    full_calls = []

    def observe_full(*args, **kwargs):
        full_calls.append(True)
        return old_full(*args, **kwargs)

    monkeypatch.setattr(
        projection_context,
        "load_contract_declaration_projection_context",
        observe_declaration,
    )
    monkeypatch.setattr(
        projection_context, "load_effect_projection_context", observe_full
    )
    declaration_context = load_candidate_projection_context(
        temp_sqlite_store, declaration
    )
    assert declaration_context.contracts == ()
    assert declaration_calls == [declaration.id]
    assert full_calls == []

    load_candidate_projection_context(temp_sqlite_store, mixed)
    assert declaration_calls == [declaration.id, mixed.id]
    assert full_calls == [True]


def test_unsupported_candidate_shape_uses_legacy_full_context(
    monkeypatch, temp_sqlite_store
):
    from aigineering.core import projection_context

    runtime = candidate_runtime(temp_sqlite_store)
    candidate = _candidate(runtime, (_asset_effect(),), key="malformed-shape")
    malformed = replace(candidate)
    object.__setattr__(malformed, "effects", [])
    assert build_asset_proposal_projection_request(malformed) is None
    calls = []
    original = projection_context.load_effect_projection_context

    def full(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(projection_context, "load_effect_projection_context", full)
    context = load_asset_proposal_projection_context(temp_sqlite_store, malformed)
    assert calls == [True]
    assert context.runtime_records == tuple(
        record for _, record in temp_sqlite_store.scan_runtime_records()
    )
    facade_context = load_candidate_projection_context(temp_sqlite_store, malformed)
    assert calls == [True, True]
    assert facade_context.runtime_records == context.runtime_records


def test_request_selects_no_unrelated_facts_and_skips_multi_effect_replay_reads(
    temp_sqlite_store,
):
    runtime = candidate_runtime(temp_sqlite_store)
    _closed_history(temp_sqlite_store, runtime, 16)
    one = _candidate(runtime, (_asset_effect(),), key="no-history-single")
    many = _candidate(
        runtime,
        (_asset_effect("first"), _asset_effect("second")),
        key="no-history-multi",
    )

    request = build_asset_proposal_projection_request(one)
    assert request is not None and request.include_candidate_replay
    selected, sql, _vm = _measure_reader(temp_sqlite_store, request)
    assert selected == AssetProposalProjectionInputs()
    assert len(sql) == 2
    assert all("runtime_records" in statement for statement, _ in sql)

    request_many = build_asset_proposal_projection_request(many)
    assert request_many is not None and not request_many.include_candidate_replay
    selected_many, sql_many, _vm_many = _measure_reader(temp_sqlite_store, request_many)
    assert selected_many == AssetProposalProjectionInputs()
    assert sql_many == []


def test_claim_bound_request_reads_exact_parent_and_preserves_missing_parent_rejection(
    temp_sqlite_store,
):
    runtime = candidate_runtime(temp_sqlite_store)
    parent = Contract(id="", outputs=("report",), budget=1)
    parent = replace(parent, id=contract_identity_v3(parent))
    assert runtime.publisher.publish(
        (contract_declaration_effect(parent),), idempotency_key="parent"
    ).accepted
    worker_key = replace(
        runtime.actor_key,
        capabilities=("worker.submit",),
    )
    binding = CandidateClaimBinding(
        contract_id=parent.id,
        claim_id="claim:test",
        claim_epoch=1,
        package_id="pkg:test",
    )
    candidate = _candidate(
        runtime,
        (_asset_effect("report", "ok", created_by=parent.id),),
        key="bound-output",
        claim_binding=binding,
    )
    request = build_asset_proposal_projection_request(candidate)
    assert request is not None and request.claim_bound_contract_id == parent.id
    selected, _sql, _vm = _measure_reader(temp_sqlite_store, request)
    assert selected.contracts == (parent,)
    assert selected.runtime_records == ()
    context = load_asset_proposal_projection_context(temp_sqlite_store, candidate)
    decision = reduce_candidate(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=(worker_key,),
        projection_context=context,
    )
    assert decision.accepted
    assert len(decision.assets) == 1
    full_decision = reduce_candidate(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=(worker_key,),
        projection_context=load_effect_projection_context(temp_sqlite_store),
    )
    assert _reduction_semantics(decision) == _reduction_semantics(full_decision)

    missing_binding = replace(binding, contract_id="task:v6:missing-parent")
    missing = _candidate(
        runtime,
        (_asset_effect("report", "ok", created_by=missing_binding.contract_id),),
        key="missing-parent",
        claim_binding=missing_binding,
    )
    missing_context = load_asset_proposal_projection_context(temp_sqlite_store, missing)
    assert missing_context.contracts == ()
    rejected = reduce_candidate(
        missing,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=(worker_key,),
        projection_context=missing_context,
    )
    assert not rejected.accepted
    rejection = next(
        r for r in rejected.runtime_records if r.record_type == "candidate.rejected"
    )
    assert "unknown Contract" in str(rejection.payload.get("reason", ""))


def test_claim_bound_rejections_match_full_context_for_output_creator_and_shape(
    temp_sqlite_store,
):
    runtime = candidate_runtime(temp_sqlite_store)
    parent = build_control_plane_contract(
        name="claim-bound-matrix",
        inputs=(),
        outputs=("report",),
        activation="",
        budget=1,
        acceptance_policy={
            "mode": "mechanical",
            "output_shapes": {"report": {"answer": "string"}},
        },
    )
    parent_decision = runtime.publisher.publish(
        (contract_declaration_effect(parent),), idempotency_key="matrix-parent"
    )
    assert parent_decision.accepted, next(
        dict(record.payload)
        for record in parent_decision.runtime_records
        if record.record_type == "candidate.rejected"
    )
    worker_key = replace(runtime.actor_key, capabilities=("worker.submit",))
    binding = CandidateClaimBinding(parent.id, "claim:matrix", 1, "pkg:matrix")
    cases = (
        (
            "undeclared-output",
            _asset_effect("other", "ok", created_by=parent.id),
        ),
        (
            "wrong-creator",
            _asset_effect("report", '{"answer":"ok"}', created_by="task:v3:wrong"),
        ),
        (
            "method-result-missing-creator",
            _asset_effect("_plan_result_result", "ok"),
        ),
        ("shape-mismatch", _asset_effect("report", "not-json", created_by=parent.id)),
    )
    for key, effect in cases:
        candidate = _candidate(runtime, (effect,), key=key, claim_binding=binding)
        narrow = load_asset_proposal_projection_context(temp_sqlite_store, candidate)
        full = load_effect_projection_context(temp_sqlite_store)

        def semantic_result(context):
            decision = reduce_candidate(
                candidate,
                runtime.genesis,
                verifier_factory=create_verifier,
                actor_keys=(worker_key,),
                projection_context=context,
            )
            return _reduction_semantics(decision)

        assert narrow.contracts == (parent,)
        assert narrow.assets == ()
        assert semantic_result(narrow) == semantic_result(full), key
        assert semantic_result(narrow)[0] is False, key


def test_claim_bound_commit_and_exact_replay_keep_writer_claim_checks(
    temp_sqlite_store,
):
    from aigineering.core.worker_routing import WorkerRegistration
    from aigineering.runtime import claim_next_package

    class ClaimWorker:
        worker_id = "worker:asset-projection"

        def registration(self):
            return WorkerRegistration(self.worker_id)

        def invoke(self, _contract, _disclosed_assets):
            raise AssertionError("this test commits the signed Candidate directly")

    runtime = candidate_runtime(temp_sqlite_store)
    contract = build_control_plane_contract(
        name="claim-bound-commit",
        inputs=(),
        outputs=("report",),
        activation="",
        budget=1,
    )
    assert runtime.publisher.publish(
        (contract_declaration_effect(contract),), idempotency_key="claim-commit-parent"
    ).accepted
    host = hosted_worker(
        temp_sqlite_store,
        ClaimWorker(),
        genesis=runtime.genesis,
        authority_key=runtime.actor_key,
        authority_signer=runtime.signer,
    )
    claimed = claim_next_package(
        temp_sqlite_store,
        worker_id=host.worker_id,
        contract_id=contract.id,
        claim_candidate=host.claim_candidate(
            request_id="asset-projection-claim", contract_id=contract.id
        ),
    )
    assert claimed is not None
    binding = CandidateClaimBinding(
        contract.id,
        claimed.package.claim_id,
        claimed.package.claim_epoch,
        claimed.package.package_id,
    )
    candidate = create_candidate_proposal(
        domain_id=host.genesis.id,
        actor_id=host.actor_key.actor_id,
        key_id=host.actor_key.key_id,
        effects=(
            _asset_effect("report", "signed worker output", created_by=contract.id),
        ),
        signer=host.signer,
        idempotency_key="claim-bound-asset-proposal",
        claim_binding=binding,
    )
    narrow_reduction = reduce_candidate(
        candidate,
        host.genesis,
        verifier_factory=create_verifier,
        actor_keys=(host.actor_key,),
        projection_context=load_asset_proposal_projection_context(
            temp_sqlite_store, candidate, actor_keys=(host.actor_key,)
        ),
    )
    full_reduction = reduce_candidate(
        candidate,
        host.genesis,
        verifier_factory=create_verifier,
        actor_keys=(host.actor_key,),
        projection_context=load_effect_projection_context(
            temp_sqlite_store, actor_keys=(host.actor_key,)
        ),
    )
    assert _reduction_semantics(narrow_reduction) == _reduction_semantics(
        full_reduction
    )
    committer = CandidateCommitter(temp_sqlite_store, temp_sqlite_store)
    decision = committer.commit(candidate, host.genesis)
    assert decision.accepted
    assert any(
        record.record_type == "claim.submitted"
        and record.payload.get("candidate_id") == candidate.id
        for _revision, record in temp_sqlite_store.scan_runtime_records(
            record_type="claim.submitted"
        )
    )
    assert temp_sqlite_store.get_claim(contract.id)["status"] == "submitted"

    replay = committer.commit(candidate, host.genesis)
    assert replay.accepted
    assert replay.assets == decision.assets
    assert temp_sqlite_store.get_claim(contract.id)["status"] == "submitted"


def test_single_effect_replay_context_contains_all_exact_heads_and_members(
    temp_sqlite_store,
):
    runtime = candidate_runtime(temp_sqlite_store)
    candidate = _candidate(runtime, (_asset_effect("replay", "same"),), key="replay")
    first = CandidateCommitter(temp_sqlite_store, temp_sqlite_store).commit(
        candidate, runtime.genesis
    )
    assert first.accepted

    context = load_asset_proposal_projection_context(temp_sqlite_store, candidate)
    assert {record.record_type for record in context.runtime_records} == {
        "candidate.received",
        "candidate.committed",
        "asset.committed",
        "asset.legacy-graph.migrated",
    }
    replay = reduce_candidate(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=(runtime.actor_key,),
        projection_context=context,
    )
    full = reduce_candidate(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=(runtime.actor_key,),
        projection_context=load_effect_projection_context(temp_sqlite_store),
    )
    assert replay.accepted == full.accepted
    assert replay.assets == full.assets
    assert replay.contracts == full.contracts
    assert replay.trace_entries == full.trace_entries
    assert tuple(record.id for record in replay.runtime_records) == tuple(
        record.id for record in full.runtime_records
    )
    selected_ids = {record.id for record in context.runtime_records}
    expected_revision_order = tuple(
        record
        for _revision, record in temp_sqlite_store.scan_runtime_records()
        if record.id in selected_ids
    )
    assert context.runtime_records == expected_revision_order
    receipt = next(
        r for r in context.runtime_records if r.record_type == "candidate.received"
    )
    head = next(
        r for r in context.runtime_records if r.record_type == "candidate.committed"
    )
    projected_records = tuple(
        record
        for record in first.runtime_records
        if record.id in set(head.payload["committed_record_ids"])
    )
    assert exact_single_effect_head_replay(
        candidate,
        receipt.id,
        projected_records,
        context.runtime_records,
    )

    changed = _candidate(
        runtime, (_asset_effect("replay", "changed"),), key="replay-changed"
    )
    changed_context = load_asset_proposal_projection_context(temp_sqlite_store, changed)
    assert changed_context.runtime_records == ()
    changed_narrow = reduce_candidate(
        changed,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=(runtime.actor_key,),
        projection_context=changed_context,
    )
    changed_full = reduce_candidate(
        changed,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=(runtime.actor_key,),
        projection_context=load_effect_projection_context(temp_sqlite_store),
    )
    assert _reduction_semantics(changed_narrow) == _reduction_semantics(changed_full)
    assert changed_narrow.assets[0].id != replay.assets[0].id


def test_selector_preserves_duplicate_receipts_and_heads_for_generic_rejection(
    temp_sqlite_store,
):
    runtime = candidate_runtime(temp_sqlite_store)
    candidate = _candidate(
        runtime, (_asset_effect("duplicate", "same"),), key="duplicate"
    )
    committed = CandidateCommitter(temp_sqlite_store, temp_sqlite_store).commit(
        candidate, runtime.genesis
    )
    assert committed.accepted
    original = load_asset_proposal_projection_context(temp_sqlite_store, candidate)
    receipt = next(
        r for r in original.runtime_records if r.record_type == "candidate.received"
    )
    head = next(
        r for r in original.runtime_records if r.record_type == "candidate.committed"
    )

    # These deliberately malformed duplicate records test selection fidelity only;
    # exact_single_effect_head_replay remains the generic proof/rejection owner.
    duplicate_receipt_payload = dict(receipt.payload)
    duplicate_receipt_payload["actor_id"] = "duplicate-untrusted-actor"
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
    with temp_sqlite_store._conn:
        temp_sqlite_store._insert_runtime_record(duplicate_receipt)
        temp_sqlite_store._insert_runtime_record(duplicate_head)

    context = load_asset_proposal_projection_context(temp_sqlite_store, candidate)
    assert (
        sum(r.record_type == "candidate.received" for r in context.runtime_records) == 2
    )
    assert (
        sum(r.record_type == "candidate.committed" for r in context.runtime_records)
        == 2
    )
    assert {
        r.id
        for r in context.runtime_records
        if r.id in head.payload["committed_record_ids"]
    } == set(head.payload["committed_record_ids"])
    projected = tuple(
        record
        for record in committed.runtime_records
        if record.id in set(head.payload["committed_record_ids"])
    )
    assert not exact_single_effect_head_replay(
        candidate, receipt.id, projected, context.runtime_records
    )


def test_malformed_replay_member_shapes_are_selected_without_becoming_proof(
    temp_sqlite_store,
):
    runtime = candidate_runtime(temp_sqlite_store)
    candidate = _candidate(runtime, (_asset_effect("members", "same"),), key="members")
    committed = CandidateCommitter(temp_sqlite_store, temp_sqlite_store).commit(
        candidate, runtime.genesis
    )
    assert committed.accepted
    original = load_asset_proposal_projection_context(temp_sqlite_store, candidate)
    receipt = next(
        record
        for record in original.runtime_records
        if record.record_type == "candidate.received"
    )
    original_head = next(
        record
        for record in original.runtime_records
        if record.record_type == "candidate.committed"
    )
    member_id = original_head.payload["committed_record_ids"][0]
    malformed_heads = (
        ("missing", ["record:missing-member"], (receipt.id, "record:missing-member")),
        ("duplicate", [member_id, member_id], (receipt.id, member_id, member_id)),
        ("opaque", {"member": member_id}, (receipt.id,)),
    )
    for label, member_ids, causal_parents in malformed_heads:
        malformed_head = create_runtime_record(
            "candidate.committed",
            {"candidate_id": candidate.id, "committed_record_ids": member_ids},
            causal_parents=causal_parents,
        )
        with temp_sqlite_store._conn:
            temp_sqlite_store._insert_runtime_record(malformed_head)
        context = load_asset_proposal_projection_context(temp_sqlite_store, candidate)
        selected = next(
            record
            for record in context.runtime_records
            if record.id == malformed_head.id
        )
        expected_member_ids = (
            tuple(member_ids) if isinstance(member_ids, list) else member_ids
        )
        assert selected.payload["committed_record_ids"] == expected_member_ids
        if label == "missing":
            assert not any(
                record.id == "record:missing-member"
                for record in context.runtime_records
            )
        if label == "opaque":
            assert len(context.runtime_records) == 7
        projected = tuple(
            record
            for record in committed.runtime_records
            if record.id in set(original_head.payload["committed_record_ids"])
        )
        assert not exact_single_effect_head_replay(
            candidate, receipt.id, projected, context.runtime_records
        )


def test_optional_reader_fallback_and_reader_errors_are_not_hidden(
    temp_sqlite_store, monkeypatch
):
    runtime = candidate_runtime(temp_sqlite_store)
    candidate = _candidate(runtime, (_asset_effect(),), key="fallback")

    class LegacyStore:
        def __init__(self, delegate):
            self.delegate = delegate

        def __getattr__(self, name):
            if name == "read_asset_proposal_projection_inputs":
                raise AttributeError(name)
            return getattr(self.delegate, name)

    full = load_asset_proposal_projection_context(
        LegacyStore(temp_sqlite_store), candidate
    )
    facade_full = load_candidate_projection_context(
        LegacyStore(temp_sqlite_store), candidate
    )
    assert full.contracts == tuple(temp_sqlite_store.get_all_contracts())
    assert facade_full.contracts == full.contracts
    assert len(full.runtime_records) == len(
        tuple(record for _revision, record in temp_sqlite_store.scan_runtime_records())
    )
    assert len(facade_full.runtime_records) == len(full.runtime_records)

    original = temp_sqlite_store.read_asset_proposal_projection_inputs
    for error in (
        sqlite3.OperationalError("reader failure"),
        TypeError("reader type failure"),
        ValueError("reader value failure"),
        RuntimeError("reader programming failure"),
    ):

        def fail(_request, error=error):
            raise error

        monkeypatch.setattr(
            temp_sqlite_store, "read_asset_proposal_projection_inputs", fail
        )
        with pytest.raises(type(error), match=str(error)):
            load_asset_proposal_projection_context(temp_sqlite_store, candidate)
        with pytest.raises(type(error), match=str(error)):
            load_candidate_projection_context(temp_sqlite_store, candidate)
    monkeypatch.setattr(
        temp_sqlite_store, "read_asset_proposal_projection_inputs", original
    )


def test_known_hint_uncertainty_falls_back_but_unknown_error_propagates(
    temp_sqlite_store, monkeypatch
):
    from aigineering.core import projection_context

    runtime = candidate_runtime(temp_sqlite_store)
    candidate = _candidate(runtime, (_asset_effect(),), key="hint-error")
    full_reads = []
    original_full = projection_context.load_effect_projection_context

    def full(*args, **kwargs):
        full_reads.append(True)
        return original_full(*args, **kwargs)

    monkeypatch.setattr(projection_context, "load_effect_projection_context", full)
    monkeypatch.setattr(
        projection_context,
        "build_asset_proposal_projection_request",
        lambda _candidate: (_ for _ in ()).throw(TypeError("hint uncertain")),
    )
    load_asset_proposal_projection_context(temp_sqlite_store, candidate)
    assert full_reads

    monkeypatch.setattr(
        projection_context,
        "build_asset_proposal_projection_request",
        lambda _candidate: (_ for _ in ()).throw(RuntimeError("unknown hint failure")),
    )
    with pytest.raises(RuntimeError, match="unknown hint failure"):
        load_asset_proposal_projection_context(temp_sqlite_store, candidate)

    for error in (
        KeyError("hint uncertain"),
        TypeError("hint uncertain"),
        ValueError("hint uncertain"),
    ):
        full_reads.clear()

        def uncertain(_candidate, error=error):
            raise error

        monkeypatch.setattr(
            projection_context, "build_asset_proposal_projection_request", uncertain
        )
        load_candidate_projection_context(temp_sqlite_store, candidate)
        assert full_reads == [True]

    monkeypatch.setattr(
        projection_context,
        "build_asset_proposal_projection_request",
        lambda _candidate: (_ for _ in ()).throw(RuntimeError("unknown facade hint")),
    )
    with pytest.raises(RuntimeError, match="unknown facade hint"):
        load_candidate_projection_context(temp_sqlite_store, candidate)


def test_owned_snapshot_and_existing_transaction_are_preserved(temp_sqlite_store):
    runtime = candidate_runtime(temp_sqlite_store)
    candidate = _candidate(runtime, (_asset_effect(),), key="snapshot")
    request = build_asset_proposal_projection_request(candidate)
    assert request is not None
    before = temp_sqlite_store._conn.total_changes

    temp_sqlite_store.read_asset_proposal_projection_inputs(request)
    assert not temp_sqlite_store._conn.in_transaction
    assert temp_sqlite_store._conn.total_changes == before

    temp_sqlite_store._conn.execute("BEGIN")
    try:
        temp_sqlite_store.read_asset_proposal_projection_inputs(request)
        assert temp_sqlite_store._conn.in_transaction
        assert temp_sqlite_store._conn.total_changes == before
    finally:
        temp_sqlite_store._conn.execute("ROLLBACK")


def test_bad_signature_still_reaches_authentication_rejection(temp_sqlite_store):
    runtime = candidate_runtime(temp_sqlite_store)
    candidate = _candidate(runtime, (_asset_effect(),), key="bad-signature")
    forged = replace(candidate, signature="not-a-signature")

    decision = CandidateCommitter(temp_sqlite_store, temp_sqlite_store).commit(
        forged, runtime.genesis
    )

    assert not decision.accepted
    rejection = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.authentication_rejected"
    )
    assert "signature" in str(rejection.payload.get("reason", ""))


def test_parent_delegated_protected_name_stays_policy_owned(temp_sqlite_store):
    runtime = candidate_runtime(temp_sqlite_store)
    parent = Contract(
        id="task:v3:placeholder",
        outputs=("_sys_report",),
        minting_authority=("_sys_report",),
        budget=1,
    )
    parent = replace(parent, id=contract_identity_v3(parent))
    assert runtime.publisher.publish(
        (contract_declaration_effect(parent),), idempotency_key="protected-parent"
    ).accepted
    worker_key = replace(
        runtime.actor_key,
        capabilities=("worker.submit",),
    )
    binding = CandidateClaimBinding(
        contract_id=parent.id,
        claim_id="claim:protected",
        claim_epoch=1,
        package_id="pkg:protected",
    )
    candidate = _candidate(
        runtime,
        (_asset_effect("_sys_report", "ok", created_by=parent.id),),
        key="protected-output",
        claim_binding=binding,
    )
    decision = reduce_candidate(
        candidate,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=(worker_key,),
        projection_context=load_asset_proposal_projection_context(
            temp_sqlite_store, candidate
        ),
    )
    assert decision.accepted

    undelegated = replace(parent, id="task:v3:placeholder", minting_authority=())
    undelegated = replace(undelegated, id=contract_identity_v3(undelegated))
    candidate_without_delegation = _candidate(
        runtime,
        (_asset_effect("_sys_report", "other", created_by=undelegated.id),),
        key="protected-without-delegation",
        claim_binding=replace(binding, contract_id=undelegated.id),
    )
    rejected = reduce_candidate(
        candidate_without_delegation,
        runtime.genesis,
        verifier_factory=create_verifier,
        actor_keys=(worker_key,),
        projection_context=EffectProjectionContext(contracts=(undelegated,)),
    )
    assert not rejected.accepted
    rejection = next(
        r for r in rejected.runtime_records if r.record_type == "candidate.rejected"
    )
    assert "asset.publish.protected" in str(rejection.payload.get("reason", ""))


def test_h_matrix_keeps_returned_objects_fixed_and_candidate_selectors_indexed(
    tmp_path, monkeypatch
):
    from aigineering.core.domain import initialize_genesis
    from aigineering.core.sqlite_store import SQLiteStore

    vm_by_h = {}
    signed_candidates = []
    signed_batches = []
    frozen_runtime = None
    for history in (0, 16, 64):
        store = SQLiteStore(str(tmp_path / f"history-{history}.sqlite"))
        try:
            if frozen_runtime is None:
                runtime = candidate_runtime(store)
                frozen_runtime = runtime
            else:
                initialize_genesis(store, frozen_runtime.genesis)
                runtime = candidate_runtime(
                    store,
                    genesis=frozen_runtime.genesis,
                    actor_key=frozen_runtime.actor_key,
                    signer=frozen_runtime.signer,
                )
            _closed_history(store, runtime, history)
            candidate = _candidate(
                runtime, (_asset_effect(),), key="missing-replay-fixed"
            )
            signed_candidates.append(candidate)
            request = build_asset_proposal_projection_request(candidate)
            assert request is not None
            decoder_counts = {"runtime": 0, "contract": 0, "asset": 0}
            for label, method_name in (
                ("runtime", "_row_to_runtime_record"),
                ("contract", "_row_to_contract"),
                ("asset", "_row_to_asset"),
            ):
                original_decoder = getattr(store, method_name)

                def count_decoder(row, *, label=label, original=original_decoder):
                    decoder_counts[label] += 1
                    return original(row)

                monkeypatch.setattr(store, method_name, count_decoder)
            selected, sql, vm = _measure_reader(store, request)
            assert selected == AssetProposalProjectionInputs()
            assert len(sql) == 2
            assert all("runtime_records" in statement for statement, _ in sql)

            batch = _candidate(
                runtime,
                tuple(_asset_effect(f"batch-{index}") for index in range(64)),
                key="asset-projection-p64-fixed",
            )
            signed_batches.append(batch)
            batch_request = build_asset_proposal_projection_request(batch)
            assert (
                batch_request is not None and not batch_request.include_candidate_replay
            )
            batch_inputs, batch_sql, _batch_vm = _measure_reader(store, batch_request)
            assert batch_inputs == AssetProposalProjectionInputs()
            assert batch_sql == []
            assert decoder_counts == {"runtime": 0, "contract": 0, "asset": 0}
            plans = []
            for record_type in ("candidate.received", "candidate.committed"):
                plans.extend(
                    store._conn.execute(
                        "EXPLAIN QUERY PLAN SELECT * FROM runtime_records WHERE "
                        "record_type = ? AND json_extract(payload_json, '$.candidate_id') = ? ORDER BY revision",
                        (record_type, candidate.id),
                    ).fetchall()
                )
            assert plans and all("SEARCH" in row[3].upper() for row in plans)
            by_record_type = {
                "candidate.received": next(
                    steps
                    for statement, steps in sql
                    if "candidate.received" in statement
                ),
                "candidate.committed": next(
                    steps
                    for statement, steps in sql
                    if "candidate.committed" in statement
                ),
            }
            vm_by_h[history] = by_record_type
            parent_plan = store._conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM contracts WHERE id = ?",
                ("task:v6:exact-parent",),
            ).fetchone()
            member_plan = store._conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM runtime_records WHERE record_id IN (?, ?)",
                ("record:one", "record:two"),
            ).fetchone()
            assert "SEARCH" in parent_plan[3].upper()
            assert "SEARCH" in member_plan[3].upper()
        finally:
            store.close()
    for selector in ("candidate.received", "candidate.committed"):
        assert vm_by_h[64][selector] <= 2 * vm_by_h[0][selector] + 64
    assert all(candidate == signed_candidates[0] for candidate in signed_candidates)
    assert all(batch == signed_batches[0] for batch in signed_batches)


def test_parent_and_fixed_replay_bundle_have_bounded_indexed_selectors(
    tmp_path, monkeypatch
):
    from aigineering.core.domain import initialize_genesis
    from aigineering.core.sqlite_store import SQLiteStore

    parent_vm = {}
    replay_vm = {}
    replay_sizes = {}
    frozen_runtime = None
    for history in (0, 16, 64):
        store = SQLiteStore(str(tmp_path / f"selector-history-{history}.sqlite"))
        try:
            if frozen_runtime is None:
                runtime = candidate_runtime(store)
                frozen_runtime = runtime
            else:
                initialize_genesis(store, frozen_runtime.genesis)
                runtime = candidate_runtime(
                    store,
                    genesis=frozen_runtime.genesis,
                    actor_key=frozen_runtime.actor_key,
                    signer=frozen_runtime.signer,
                )
            parent = build_control_plane_contract(
                name="selector-exact-parent",
                inputs=(),
                outputs=(),
                activation="",
                budget=1,
            )
            assert runtime.publisher.publish(
                (contract_declaration_effect(parent),),
                idempotency_key="selector-parent",
            ).accepted
            _closed_history(store, runtime, history)

            bound = _candidate(
                runtime,
                (_asset_effect("report", "ok", created_by=parent.id),),
                key="selector-bound-no-history",
                claim_binding=CandidateClaimBinding(
                    parent.id, "claim:selector", 1, "pkg:selector"
                ),
            )
            bound_request = build_asset_proposal_projection_request(bound)
            assert bound_request is not None
            decoder_counts = {"runtime": 0, "contract": 0, "asset": 0}
            for label, method_name in (
                ("runtime", "_row_to_runtime_record"),
                ("contract", "_row_to_contract"),
                ("asset", "_row_to_asset"),
            ):
                original_decoder = getattr(store, method_name)

                def count_decoder(row, *, label=label, original=original_decoder):
                    decoder_counts[label] += 1
                    return original(row)

                monkeypatch.setattr(store, method_name, count_decoder)
            selected_bound, bound_sql, _ = _measure_reader(store, bound_request)
            assert selected_bound.contracts == (parent,)
            assert selected_bound.runtime_records == ()
            assert decoder_counts == {"runtime": 0, "contract": 1, "asset": 0}
            assert len(bound_sql) == 3
            parent_entry = next(item for item in bound_sql if "contracts" in item[0])
            parent_vm[history] = parent_entry[1]
            parent_plan = store._conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM contracts WHERE id = ?",
                (parent.id,),
            ).fetchone()
            assert "SEARCH" in parent_plan[3].upper()

            replay_candidate = _candidate(
                runtime,
                (_asset_effect("fixed-replay", "same"),),
                key="selector-fixed-replay",
            )
            committed = CandidateCommitter(store, store).commit(
                replay_candidate, runtime.genesis
            )
            assert committed.accepted
            replay_request = build_asset_proposal_projection_request(replay_candidate)
            assert replay_request is not None
            decoder_counts = {"runtime": 0, "contract": 0, "asset": 0}
            selected_replay, replay_sql, _ = _measure_reader(store, replay_request)
            assert len(selected_replay.runtime_records) == 4
            assert decoder_counts == {"runtime": 4, "contract": 0, "asset": 0}
            assert len(replay_sql) == 3
            replay_sizes[history] = len(selected_replay.runtime_records)
            replay_vm[history] = {
                "candidate.received": next(
                    steps
                    for statement, steps in replay_sql
                    if "candidate.received" in statement
                ),
                "candidate.committed": next(
                    steps
                    for statement, steps in replay_sql
                    if "candidate.committed" in statement
                ),
                "members": next(
                    steps
                    for statement, steps in replay_sql
                    if "record_id IN" in statement
                ),
            }
            member_plan = store._conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM runtime_records WHERE record_id IN (?, ?)",
                ("record:one", "record:two"),
            ).fetchone()
            assert "SEARCH" in member_plan[3].upper()
        finally:
            store.close()

    assert replay_sizes == {0: 4, 16: 4, 64: 4}
    for history in (16, 64):
        assert parent_vm[history] <= 2 * parent_vm[0] + 64
        for selector in ("candidate.received", "candidate.committed", "members"):
            assert replay_vm[history][selector] <= 2 * replay_vm[0][selector] + 64


def test_multi_effect_commit_uses_new_context_without_changing_asset_facts(
    temp_sqlite_store, monkeypatch
):
    from aigineering.core import projection_context

    runtime = candidate_runtime(temp_sqlite_store)
    candidate = _candidate(
        runtime,
        (_asset_effect("batch-one"), _asset_effect("batch-two")),
        key="batch-commit",
    )
    requests = []
    reader = temp_sqlite_store.read_asset_proposal_projection_inputs

    def observe(request):
        requests.append(request)
        return reader(request)

    monkeypatch.setattr(
        temp_sqlite_store, "read_asset_proposal_projection_inputs", observe
    )
    full_calls = []
    old_full = projection_context.load_effect_projection_context

    def full(*args, **kwargs):
        full_calls.append(True)
        return old_full(*args, **kwargs)

    monkeypatch.setattr(projection_context, "load_effect_projection_context", full)
    decision = CandidateCommitter(
        temp_sqlite_store,
        temp_sqlite_store,
        additional_effects={"custom.unused": ("asset.publish", lambda *_args: None)},
    ).commit(candidate, runtime.genesis)
    assert decision.accepted
    assert len(decision.assets) == 2
    assert requests and requests[0].candidate_id == candidate.id
    assert requests[0].include_candidate_replay is False
    assert full_calls == []
    assert len(temp_sqlite_store.get_all_assets()) == 2
    assert not any(
        record.record_type
        in {
            "contract.declared",
            "asset.definition-content.asserted",
            "allowance.granted",
            "allowance.returned",
        }
        for record in decision.runtime_records
    )


def test_graph_and_mixed_candidate_shapes_keep_full_context(
    monkeypatch, temp_sqlite_store
):
    from aigineering.core import projection_context

    runtime = candidate_runtime(temp_sqlite_store)
    contract = build_control_plane_contract(
        name="fallback-contract", inputs=(), outputs=(), activation="", budget=1
    )
    candidates = (
        _candidate(
            runtime,
            (_asset_effect("ordinary"), contract_declaration_effect(contract)),
            key="mixed",
        ),
        _candidate(
            runtime,
            (_asset_effect("ordinary"), CandidateEffect("asset.assert", {})),
            key="graph",
        ),
        _candidate(
            runtime,
            (_asset_effect("ordinary"), CandidateEffect("custom.noop", {})),
            key="custom-mixed",
        ),
    )
    calls = []
    old = projection_context.load_effect_projection_context

    def full(*args, **kwargs):
        calls.append(True)
        return old(*args, **kwargs)

    monkeypatch.setattr(projection_context, "load_effect_projection_context", full)
    for candidate in candidates:
        context = load_asset_proposal_projection_context(temp_sqlite_store, candidate)
        assert context.assets == tuple(temp_sqlite_store.get_all_assets())
    assert len(calls) == 3


def test_asset_materialization_rebuilds_and_reopens_without_changing_seals(tmp_path):
    from aigineering.core.sqlite_store import SQLiteStore

    path = str(tmp_path / "asset-projection-rebuild.sqlite")
    store = SQLiteStore(path)
    runtime = candidate_runtime(store)
    candidate = _candidate(
        runtime, (_asset_effect("reopen", "committed"),), key="reopen-asset"
    )
    decision = CandidateCommitter(store, store).commit(candidate, runtime.genesis)
    assert decision.accepted
    expected_assets = tuple(store.get_all_assets())
    assert len(expected_assets) == 1
    expected_head = next(
        record
        for _revision, record in store.scan_runtime_records(
            record_type="candidate.committed"
        )
        if record.payload.get("candidate_id") == candidate.id
    )
    before = store.rebuild_runtime_materializations(strict=True)
    store.close()

    reopened = SQLiteStore(path)
    assert reopened.rebuild_runtime_materializations(strict=True) == before
    assert tuple(reopened.get_all_assets()) == expected_assets
    persisted_head = next(
        record
        for _revision, record in reopened.scan_runtime_records(
            record_type="candidate.committed"
        )
        if record.payload.get("candidate_id") == candidate.id
    )
    assert persisted_head == expected_head
    reopened.close()
