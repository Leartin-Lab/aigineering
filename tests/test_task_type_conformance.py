"""One conformance standard for every v0.5.0 runtime work-unit type.

These tests deliberately keep ``TaskTypeSpec`` in the verification suite: it
is a release oracle, not a second runtime dispatcher or prompt framework.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from aigineering.agent.prompt import contract_prompt
from aigineering.core.ids import hash_contract_v3, validate_contract_identity
from aigineering.core.method_handlers.recovery import schedule_projection_recovery
from aigineering.core.method_runtime import MethodRuntime
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.methods import (
    continuation_contract,
    method_contract,
    retry_contract,
)
from aigineering.core.projection import project_candidate
from aigineering.core.store import MemoryStore
from aigineering.core.sufficiency import sufficiency_result_asset
from aigineering.core.trace import TraceStore
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.types import Candidate, Contract, ProjectionStatus


@dataclass(frozen=True)
class TaskTypeSpec:
    """Uniform release oracle for one work-unit or diagnostic type."""

    name: str
    origin: str
    parent_relation: str
    expected_action: str | None
    protocol_asset_prefix: str | None
    label: str | None
    required_capability: str | None = None
    schedulable: bool = True


TASK_TYPE_SPECS = {
    "root": TaskTypeSpec("root", "human", "none", "/exec|method", None, None),
    "plan": TaskTypeSpec(
        "plan", "system", "method-child", "/exec", "_method_ctx_", "method:plan"
    ),
    "replan": TaskTypeSpec(
        "replan",
        "system",
        "method-child",
        "/exec",
        "_method_ctx_",
        "method:replan",
    ),
    "tool": TaskTypeSpec(
        "tool",
        "system",
        "method-child",
        "system-exec",
        "_method_ctx_",
        "method:tool",
        "tool-execution",
    ),
    "fail": TaskTypeSpec(
        "fail", "system", "method-child", "system-exec", "_method_ctx_", "method:fail"
    ),
    "retry": TaskTypeSpec("retry", "retry", "replacement", "/exec|method", None, None),
    "recovery": TaskTypeSpec(
        "recovery", "recovery", "replacement", "/exec", "_fail_context_", None
    ),
    "continuation": TaskTypeSpec(
        "continuation",
        "continuation",
        "method-continuation",
        "/exec|method",
        "trace:method_context",
        None,
    ),
    # Sufficiency verification is intentionally an immutable system artifact,
    # not independently claimable work.  Listing it prevents a phantom task
    # type from entering release claims.
    "verification": TaskTypeSpec(
        "verification",
        "system",
        "diagnostic-asset",
        None,
        "_sufficiency_result_",
        None,
        schedulable=False,
    ),
}


def _root_contract() -> Contract:
    fields = {
        "name": "review",
        "description": "Produce the declared report from disclosed evidence.",
        "inputs": ["evidence"],
        "outputs": ["report"],
        "activation": "evidence",
        "budget": 4,
        "tool_scope": ["lookup"],
        "labels": ["review-policy"],
        "worker_capabilities": ["text"],
        "worker_pools": ["default"],
        "origin": "human",
        "parent_id": None,
        "minting_authority": [],
        "sensitive_input_policy": {"required_trust_tier": "configured"},
    }
    return Contract(id=hash_contract_v3(**fields), **fields)


def _contracts_by_type() -> dict[str, Contract]:
    root = _root_contract()
    actions = {
        "plan": WorkerAction(type="plan", payload={"reason": "decompose"}),
        "replan": WorkerAction(type="replan", payload={"reason": "invalid path"}),
        "tool": WorkerAction(
            type="tool", payload={"name": "lookup", "args": {"key": "x"}}
        ),
        "fail": WorkerAction(type="fail", payload={"reason": "cannot proceed"}),
    }
    generated = {
        name: method_contract(root, action) for name, action in actions.items()
    }
    generated["root"] = root
    generated["retry"] = retry_contract(root)
    generated["continuation"] = continuation_contract(
        root,
        generated["tool"],
        method="tool",
        budget=3,
    )

    store = MemoryStore()
    trace = TraceStore()
    runtime = MethodRuntime(
        store,
        trace,
        {root.id: root.budget},
        ingress=RuntimeIngress(store, trace),
    )
    runtime.add_contract(root)
    recovery = schedule_projection_recovery(
        runtime,
        failed_contract=root,
        candidate_raw='/exec {"outputs":{"wrong":"bad"}}',
        rejections=[
            {
                "category": "authority_rejection",
                "name": "wrong",
                "reject_reason": "undeclared output",
            }
        ],
    )
    assert recovery is not None
    generated["recovery"] = recovery
    return generated


def test_catalog_covers_every_v050_work_unit_and_diagnostic_type():
    assert set(TASK_TYPE_SPECS) == {
        "root",
        "plan",
        "replan",
        "tool",
        "retry",
        "fail",
        "recovery",
        "continuation",
        "verification",
    }
    assert all(spec.name == name for name, spec in TASK_TYPE_SPECS.items())


@pytest.mark.parametrize(
    "task_type", [name for name, spec in TASK_TYPE_SPECS.items() if spec.schedulable]
)
def test_schedulable_types_have_security_complete_identity_and_relationship(task_type):
    contract = _contracts_by_type()[task_type]
    spec = TASK_TYPE_SPECS[task_type]

    validate_contract_identity(contract)
    assert contract.origin == spec.origin
    if spec.parent_relation == "none":
        assert contract.parent_id is None
    elif spec.parent_relation in {"method-child", "method-continuation"}:
        assert contract.parent_id == _root_contract().id
    else:
        assert contract.id != _root_contract().id

    if spec.label is not None:
        assert spec.label in contract.labels
    if spec.required_capability is not None:
        assert spec.required_capability in contract.worker_capabilities
    if spec.protocol_asset_prefix == "_method_ctx_":
        context = f"_method_ctx_{_root_contract().id}"
        assert contract.activation == context
        assert context in contract.minting_authority
    elif spec.protocol_asset_prefix == "_fail_context_":
        assert any(name.startswith("_fail_context_") for name in contract.inputs)
        assert any(
            name.startswith("_fail_context_") for name in contract.minting_authority
        )
    elif spec.protocol_asset_prefix == "trace:method_context":
        assert contract.inputs == ()
        assert contract.activation == ""


@pytest.mark.parametrize(
    "task_type", [name for name, spec in TASK_TYPE_SPECS.items() if spec.schedulable]
)
def test_every_worker_output_slot_has_positive_and_wrong_name_controls(task_type):
    contract = _contracts_by_type()[task_type]
    output = contract.outputs[0]

    accepted = project_candidate(
        contract,
        Candidate(
            worker_id="conformance-worker",
            raw_output=f'/exec {{"outputs":{{"{output}":"valid evidence"}}}}',
        ),
    )
    rejected = project_candidate(
        contract,
        Candidate(
            worker_id="conformance-worker",
            raw_output='/exec {"outputs":{"undeclared_output":"invalid"}}',
        ),
    )

    assert accepted.status is ProjectionStatus.ACCEPTED
    assert [asset.name for asset in accepted.accepted_assets] == [output]
    assert rejected.status is ProjectionStatus.REJECTED
    assert rejected.accepted_assets == ()
    assert rejected.rejected_candidates[0].name == "undeclared_output"


@pytest.mark.parametrize(
    "task_type", [name for name, spec in TASK_TYPE_SPECS.items() if spec.schedulable]
)
def test_every_output_schema_has_missing_extra_and_invalid_action_controls(task_type):
    contract = _contracts_by_type()[task_type]
    output = contract.outputs[0]

    missing = project_candidate(
        contract,
        Candidate(worker_id="conformance-worker", raw_output='/exec {"outputs":{}}'),
    )
    extra = project_candidate(
        contract,
        Candidate(
            worker_id="conformance-worker",
            raw_output="/exec "
            + json.dumps(
                {"outputs": {output: "valid", "undeclared_output": "invalid"}}
            ),
        ),
    )
    invalid_action = project_candidate(
        contract,
        Candidate(
            worker_id="conformance-worker",
            raw_output='/approve {"outputs":{"report":"invalid"}}',
        ),
    )

    assert missing.status is ProjectionStatus.REJECTED
    assert missing.rejected_candidates[0].name == "(empty)"
    assert extra.status is ProjectionStatus.PARTIAL
    assert [asset.name for asset in extra.accepted_assets] == [output]
    assert [item.name for item in extra.rejected_candidates] == ["undeclared_output"]
    assert invalid_action.status is ProjectionStatus.REJECTED
    assert invalid_action.rejected_candidates[0].name == "(action)"


@pytest.mark.parametrize(
    "task_type", ["root", "plan", "replan", "retry", "recovery", "continuation"]
)
def test_worker_facing_types_receive_exact_output_protocol(task_type):
    contract = _contracts_by_type()[task_type]
    prompt = contract_prompt(contract, [])

    for output in contract.outputs:
        assert output in prompt
    assert "Use only declared output names" not in prompt  # system-only rule
    assert '/exec {"outputs"' in prompt
    if task_type in {"plan", "replan"}:
        assert "Planner result protocol (required):" in prompt
        assert "`contracts` array" in prompt


def test_verification_is_an_asset_diagnostic_not_hidden_claimable_state():
    contract = _root_contract()
    asset = sufficiency_result_asset(contract, MemoryStore())
    spec = TASK_TYPE_SPECS["verification"]

    assert spec.schedulable is False
    assert asset.name == f"{spec.protocol_asset_prefix}{contract.id}"
    assert asset.origin == spec.origin
    assert asset.created_by == contract.id


def test_security_policy_is_inherited_by_retry_recovery_and_continuation():
    contracts = _contracts_by_type()
    root = contracts["root"]

    for task_type in ("retry", "recovery", "continuation"):
        derived = contracts[task_type]
        assert derived.worker_capabilities == root.worker_capabilities
        assert derived.worker_pools == root.worker_pools
        assert dict(derived.sensitive_input_policy or {}) == dict(
            root.sensitive_input_policy or {}
        )
        assert set(derived.tool_scope).issubset(root.tool_scope)
