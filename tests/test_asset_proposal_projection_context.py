"""Narrow immutable context reads for built-in Asset proposals."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aigineering.core.commitment import CandidateCommitter, reduce_candidate
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.ids import contract_identity_v3
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
            outputs=("closed-history-result",),
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
        assert runtime.publisher.publish(
            (_asset_effect("closed-history-result"),),
            idempotency_key=f"asset-projection-history-result-{count}",
        ).accepted


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
        assert request == AssetProposalProjectionRequest(claim_bound_contract_id=None)
        assert build_asset_proposal_projection_request(many) == request

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


def test_fresh_request_selects_no_unrelated_facts_for_single_or_multi_effect(
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
    assert request == AssetProposalProjectionRequest(claim_bound_contract_id=None)
    selected, sql, _vm = _measure_reader(temp_sqlite_store, request)
    assert selected == AssetProposalProjectionInputs()
    assert sql == []

    request_many = build_asset_proposal_projection_request(many)
    assert request_many == request
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


def test_claim_bound_commit_and_generic_replay_keep_writer_claim_checks(
    temp_sqlite_store, monkeypatch
):
    from aigineering.core.worker_routing import WorkerRegistration
    from aigineering.runtime import claim_next_package
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(
        runtime_services, "sync_runtime", lambda *_args, **_kwargs: None
    )

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

    revision_before_replay = temp_sqlite_store.get_runtime_revision()
    original_head = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.committed"
    )
    replay = committer.commit(candidate, host.genesis)
    assert replay.accepted
    assert replay.assets == decision.assets
    replayed_head = next(
        record
        for record in replay.runtime_records
        if record.record_type == "candidate.committed"
    )
    assert replayed_head.id == original_head.id
    assert temp_sqlite_store.get_runtime_revision() == revision_before_replay
    assert temp_sqlite_store.get_claim(contract.id)["status"] == "submitted"


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


def test_signed_multi_effect_commit_preserves_asset_facts(
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
    decision = CandidateCommitter(temp_sqlite_store, temp_sqlite_store).commit(
        candidate, runtime.genesis
    )
    assert decision.accepted
    assert len(decision.assets) == 2
    assert requests == [AssetProposalProjectionRequest(claim_bound_contract_id=None)]
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
