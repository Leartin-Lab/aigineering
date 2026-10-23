"""Stage labels route only the active planning Contract, not method children."""

from __future__ import annotations

import json
import pytest

from conftest import candidate_runtime, hosted_worker

from aigineering.agent.prompt import contract_prompt
from aigineering.agent.tool_worker import ToolWorker
from aigineering.agent.worker import (
    WorkerExecutionError,
    compile_worker_envelope,
)
from aigineering.application import default_completion_registry
from aigineering.core.candidate_publisher import CandidatePublisherRegistry
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.delegation import (
    TaskDelegationPlugin,
    _legacy_claimed_method_contract,
    _parallel_tool_contracts,
)
from aigineering.plugins.planning_roles import (
    PLANNING_COMPILE_LABELS,
    PLANNING_STAGE_LABELS,
    active_planning_stage,
)
from aigineering.plugins.staged_planning import StagedPlanningPlugin
from aigineering.plugins.base import PluginRequest
from aigineering.plugins.task_semantics import method_contract, retry_contract
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.types import Candidate, Contract, ToolSpec
from aigineering.runtime import (
    claim_next_package,
    execute_claimed_package,
    process_task_completions,
)


class _CompileWorker:
    worker_id = "worker:stage-compiler"

    def invoke(self, contract, disclosed_assets):
        del disclosed_assets
        if contract.origin == "continuation":
            blueprint = {
                "contracts": [
                    {
                        "name": "publish-report",
                        "description": "Publish the report after using the tool.",
                        "inputs": [],
                        "outputs": ["final_report"],
                        "activation": "",
                        "budget": 1,
                        "tool_scope": [],
                        "labels": [],
                        "capability_needs": [],
                        "pool_needs": [],
                        "delegation_capabilities": [],
                        "delegation_pools": [],
                    }
                ]
            }
            output = {"planning_blueprint": json.dumps(blueprint, sort_keys=True)}
            return Candidate(
                worker_id=self.worker_id,
                raw_output="/exec " + json.dumps({"outputs": output}),
            )
        return Candidate(
            worker_id=self.worker_id,
            raw_output='/tool {"name":"lookup","args":{}}',
        )

    def registration(self):
        return WorkerRegistration(self.worker_id)


def _compile_contract() -> Contract:
    return build_control_plane_contract(
        name="planning-compile",
        description=json.dumps(
            {"allowed_inputs": [], "mode": "plan", "stage": "compile"}
        ),
        inputs=(),
        outputs=("final_report",),
        activation="",
        budget=8,
        tool_scope=("lookup",),
        labels=("plugin:plan.compile",),
        delegation_capabilities=("tool_calling",),
    )


def test_signed_compile_tool_observation_continuation_and_blueprint(monkeypatch):
    import aigineering.runtime_services

    monkeypatch.setattr(
        aigineering.runtime_services,
        "sync_runtime",
        lambda *_args, **_kwargs: None,
    )
    store = SQLiteStore(":memory:")
    authority_signer = Ed25519Signer()
    authority_key = ActorKey(
        "plugin:test-planning",
        "planning-1",
        authority_signer.kind,
        authority_signer.signer_id,
        (
            "actor.authorize",
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
            "worker.register",
        ),
    )
    genesis = create_genesis_manifest(
        "planning-stage-role-test", (authority_key,), "policy:planning-stage-role"
    )
    initialize_genesis(store, genesis)
    ingress = candidate_runtime(
        store, genesis=genesis, actor_key=authority_key, signer=authority_signer
    )
    stage = ingress.accept_contract(_compile_contract())
    descriptor = create_tool_descriptor(
        "lookup",
        "Return a deterministic observation.",
        {"type": "object"},
        trust_tier="configured",
    )
    ingress.accept_asset(descriptor, source="capability", allow_protected=True)

    compile_worker = _CompileWorker()
    compile_host = hosted_worker(
        store,
        compile_worker,
        genesis=genesis,
        authority_key=authority_key,
        authority_signer=authority_signer,
    )
    claim = claim_next_package(
        store, worker_id=compile_host.worker_id, contract_id=stage.id
    )
    assert claim is not None
    result = execute_claimed_package(claim, compile_host, store)
    assert result["status"] == "task_delegated", result
    method = store.get_contract(result["child_contract_id"])
    assert method is not None
    assert not set(method.labels) & PLANNING_STAGE_LABELS
    assert "plugin:tool" in method.labels
    assert active_planning_stage(method) is None

    tools = ToolWorker(_tool_registry(), worker_id="tool_worker:planning-stage-role")
    tool_host = hosted_worker(
        store,
        tools,
        genesis=genesis,
        authority_key=authority_key,
        authority_signer=authority_signer,
    )
    tool_claim = claim_next_package(
        store, worker_id=tool_host.worker_id, contract_id=method.id
    )
    assert tool_claim is not None
    tool_result = execute_claimed_package(tool_claim, tool_host, store)
    assert tool_result["status"] == "accepted", tool_result
    observations = store.get_assets_by_name(method.outputs[0])
    assert len(observations) == 1
    assert json.loads(observations[0].content)["ok"] is True

    publishers = CandidatePublisherRegistry(
        (("continuation.publish.v1", ingress.publisher),)
    )
    assert process_task_completions(
        store, default_completion_registry(), candidate_publishers=publishers
    ) == [method.id]
    continuation = next(
        item
        for item in store.get_all_contracts()
        if item.origin == "continuation" and item.parent_id == stage.id
    )
    assert "plugin:plan.compile" in continuation.labels
    assert active_planning_stage(continuation) == "plugin:plan.compile"
    assert continuation.tool_scope == ()
    assert "Planning compiler protocol (required)" in contract_prompt(continuation, [])

    final_claim = claim_next_package(
        store, worker_id=compile_host.worker_id, contract_id=continuation.id
    )
    assert final_claim is not None
    final_result = execute_claimed_package(final_claim, compile_host, store)
    assert final_result["status"] == "task_delegated", final_result
    assert any(
        item.name == "publish-report"
        for item in store.get_all_contracts()
        if item.parent_id == continuation.id
    )
    store.close()


def _tool_registry():
    from aigineering.core.tools import ToolRegistry

    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup"), lambda _args: "observed-value")
    return registry


def test_method_child_role_precedes_legacy_mixed_stage_labels():
    parent = _compile_contract()
    legacy = _legacy_claimed_method_contract(
        parent,
        WorkerAction(type="fail", payload={"reason": "not recoverable"}),
    )

    assert set(legacy.labels) & PLANNING_STAGE_LABELS == {"plugin:plan.compile"}
    assert active_planning_stage(legacy) is None
    assert "Planning compiler protocol (required)" not in contract_prompt(legacy, [])

    old_base_shape = method_contract(
        parent,
        WorkerAction(type="tool", payload={"name": "lookup", "args": {}}),
    )
    assert "method:tool" in old_base_shape.labels
    assert active_planning_stage(old_base_shape) is None

    legacy_tool = _legacy_claimed_method_contract(
        parent,
        WorkerAction(type="tool", payload={"name": "lookup", "args": {}}),
    )
    legacy_tool_retry = retry_contract(legacy_tool)
    assert legacy_tool_retry.origin == "retry"
    assert active_planning_stage(legacy_tool_retry) is None

    compile_retry = retry_contract(parent)
    assert compile_retry.origin == "retry"
    assert active_planning_stage(compile_retry) == "plugin:plan.compile"

    untyped_repair = Contract(
        id="task:untyped-projection-repair",
        name="untyped-projection-repair",
        description='{"repair":"projection_repair"}',
        outputs=("final_report",),
        labels=("plugin:tool", "plugin:plan.compile"),
        origin="recovery",
    )
    assert active_planning_stage(untyped_repair) == "plugin:plan.compile"


def test_compile_fail_child_exec_is_not_reinterpreted_as_blueprint():
    parent = _compile_contract()
    fail_effect = (
        TaskDelegationPlugin()
        .propose_claimed(
            parent,
            WorkerAction(type="fail", payload={"reason": "evidence unavailable"}),
            allowance=parent.budget,
        )
        .effects[0]
    )
    fail_child = Contract(**fail_effect.payload["contract"])
    signer = Ed25519Signer()
    actor = ActorKey(
        "worker:fail-child",
        "fail-child-1",
        signer.kind,
        signer.signer_id,
        ("worker.submit",),
    )
    envelope = CandidateEnvelope(
        contract_id=fail_child.id,
        worker_id=actor.actor_id,
        raw_output="/exec "
        + json.dumps(
            {"outputs": {fail_child.outputs[0]: '{"reason":"evidence unavailable"}'}}
        ),
        idempotency_key="fail-child-result",
        package_id="pkg:fail-child",
        claim_id="claim:fail-child",
        claim_epoch=1,
    )

    assert "plugin:fail" in fail_child.labels
    assert active_planning_stage(fail_child) is None
    candidate = compile_worker_envelope(
        envelope,
        domain_id="domain:fail-child",
        actor_key=actor,
        signer=signer,
        contract=fail_child,
    )
    assert "asset.assert" in {effect.effect_type for effect in candidate.effects}
    assert "contract.declare" not in {
        effect.effect_type for effect in candidate.effects
    }


def test_legacy_ambiguous_compile_labels_keep_blueprint_compiler_fence():
    signer = Ed25519Signer()
    actor = ActorKey(
        "worker:legacy-compiler",
        "legacy-compiler-1",
        signer.kind,
        signer.signer_id,
        ("worker.submit",),
    )
    contract = Contract(
        id="task:legacy-compile-recovery",
        name="legacy-compile-recovery",
        description='{"recovery_context":"old compile task"}',
        outputs=("final_report",),
        labels=("plugin:plan.compile", "plugin:replan.compile"),
        origin="recovery",
    )
    envelope = CandidateEnvelope(
        contract_id=contract.id,
        worker_id=actor.actor_id,
        raw_output='/exec {"outputs":{"final_report":"not a blueprint"}}',
        idempotency_key="legacy-compile-recovery",
        package_id="pkg:legacy-compile-recovery",
        claim_id="claim:legacy-compile-recovery",
        claim_epoch=1,
    )

    assert active_planning_stage(contract) in PLANNING_STAGE_LABELS
    assert "Replanning compiler protocol (required)" in contract_prompt(contract, [])
    try:
        compile_worker_envelope(
            envelope,
            domain_id="domain:legacy-compile",
            actor_key=actor,
            signer=signer,
            contract=contract,
        )
    except WorkerExecutionError as exc:
        assert exc.code == "planning_compile_rejected:outputs"
    else:
        raise AssertionError("legacy compile recovery bypassed blueprint validation")


def test_malformed_method_or_stage_metadata_cannot_bypass_compile_fence():
    for value in (None, {}, [], 1):
        contract = Contract(
            id=f"task:malformed-stage:{type(value).__name__}",
            name="malformed-stage-recovery",
            description=json.dumps({"method": value, "stage": value, "mode": value}),
            outputs=("final_report",),
            labels=("plugin:plan.compile", "plugin:replan.compile"),
            origin="recovery",
        )
        assert active_planning_stage(contract) in PLANNING_COMPILE_LABELS

    mismatched_description = Contract(
        id="task:mismatched-stage-metadata",
        name="mismatched-stage-metadata",
        description='{"mode":"plan","stage":"draft"}',
        outputs=("final_report",),
        labels=("plugin:plan.compile", "plugin:replan.draft"),
        origin="recovery",
    )
    assert active_planning_stage(mismatched_description) == "plugin:plan.compile"

    malformed_mode = Contract(
        id="task:malformed-stage-mode",
        name="malformed-stage-mode",
        description='{"mode":[],"stage":"draft"}',
        outputs=("final_report",),
        labels=("plugin:plan.compile", "plugin:replan.draft"),
        origin="recovery",
    )
    assert active_planning_stage(malformed_mode) == "plugin:plan.compile"


def test_active_compile_stage_still_rejects_non_blueprint_exec():
    signer = Ed25519Signer()
    actor = ActorKey(
        "worker:current-compiler",
        "current-compiler-1",
        signer.kind,
        signer.signer_id,
        ("worker.submit",),
    )
    contract = _compile_contract()
    envelope = CandidateEnvelope(
        contract_id=contract.id,
        worker_id=actor.actor_id,
        raw_output='/exec {"outputs":{"final_report":"not a blueprint"}}',
        idempotency_key="current-compile-invalid-output",
        package_id="pkg:current-compile-invalid-output",
        claim_id="claim:current-compile-invalid-output",
        claim_epoch=1,
    )

    try:
        compile_worker_envelope(
            envelope,
            domain_id="domain:current-compile",
            actor_key=actor,
            signer=signer,
            contract=contract,
        )
    except WorkerExecutionError as exc:
        assert exc.code == "planning_compile_rejected:outputs"
    else:
        raise AssertionError("active compile stage accepted a non-blueprint output")


@pytest.mark.parametrize(
    "description",
    ['{"mode":"plan","stage":"draft"}', '{"mode":[],"stage":"draft"}'],
)
def test_ambiguous_compile_label_still_rejects_final_report_exec(description):
    signer = Ed25519Signer()
    actor = ActorKey(
        "worker:ambiguous-compiler",
        "ambiguous-compiler-1",
        signer.kind,
        signer.signer_id,
        ("worker.submit",),
    )
    contract = Contract(
        id="task:ambiguous-compile-fence",
        name="ambiguous-compile-fence",
        description=description,
        outputs=("final_report",),
        labels=("plugin:plan.compile", "plugin:replan.draft"),
        origin="recovery",
    )
    envelope = CandidateEnvelope(
        contract_id=contract.id,
        worker_id=actor.actor_id,
        raw_output='/exec {"outputs":{"final_report":"not a blueprint"}}',
        idempotency_key="ambiguous-compile-fence",
        package_id="pkg:ambiguous-compile-fence",
        claim_id="claim:ambiguous-compile-fence",
        claim_epoch=1,
    )

    assert active_planning_stage(contract) == "plugin:plan.compile"
    try:
        compile_worker_envelope(
            envelope,
            domain_id="domain:ambiguous-compile-fence",
            actor_key=actor,
            signer=signer,
            contract=contract,
        )
    except WorkerExecutionError as exc:
        assert exc.code == "planning_compile_rejected:outputs"
    else:
        raise AssertionError("ambiguous compile output bypassed blueprint validation")


def test_nested_planning_stages_keep_only_the_current_stage_control_label():
    parent = _compile_contract()
    stages = StagedPlanningPlugin().stages(PluginRequest(parent=parent, allowance=8))

    for contract in stages.contracts:
        assert set(contract.labels) & PLANNING_STAGE_LABELS == {
            f"plugin:plan.{json.loads(contract.description)['stage']}"
        }
        assert active_planning_stage(contract) in PLANNING_STAGE_LABELS


def test_parallel_tool_items_drop_stage_labels_while_join_retains_stage():
    parent = build_control_plane_contract(
        name="parallel-planning-compile",
        description=json.dumps(
            {"allowed_inputs": [], "mode": "plan", "stage": "compile"}
        ),
        outputs=("final_report",),
        budget=8,
        tool_scope=("lookup", "lookup2"),
        labels=("plugin:plan.compile",),
        delegation_capabilities=("tool_calling",),
    )
    action = WorkerAction(
        type="parallel_tool",
        payload={
            "calls": [
                {"id": "first", "name": "lookup", "args": {}},
                {"id": "second", "name": "lookup2", "args": {}},
            ]
        },
    )

    children = _parallel_tool_contracts(parent, action, allowance=8)

    assert len(children) == 3
    items, continuation = children[:2], children[2]
    assert all("plugin:parallel_tool_item" in item.labels for item in items)
    assert all(not set(item.labels) & PLANNING_STAGE_LABELS for item in items)
    assert all(active_planning_stage(item) is None for item in items)
    assert "plugin:plan.compile" in continuation.labels
    assert active_planning_stage(continuation) == "plugin:plan.compile"
