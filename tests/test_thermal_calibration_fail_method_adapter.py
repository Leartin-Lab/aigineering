"""Regression coverage for the example's claimed failure-method adapter."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.ids import compute_content_hash
from aigineering.plugins.delegation import TaskDelegationPlugin
from aigineering.plugins.task_semantics import method_contract
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.wire import contract_from_dict
from examples.thermal_calibration.workers import (
    CalibrationVerifier,
    _fail_method,
)


def _current_claimed_fail_contract():
    parent = build_control_plane_contract(
        name="adapter-parent",
        description=json.dumps({"purpose": "focused adapter regression"}),
        outputs=("report",),
        budget=2,
    )
    proposal = TaskDelegationPlugin().propose_claimed(
        parent,
        WorkerAction(type="fail", payload={"reason": "source failed safely"}),
        allowance=1,
    )
    assert len(proposal.effects) == 1
    return parent, contract_from_dict(proposal.effects[0].payload["contract"])


def _with_description(contract, change):
    description = json.loads(contract.description)
    change(description)
    encoded = json.dumps(description, sort_keys=True)
    return replace(
        contract,
        description=encoded,
        outputs=(f"failure_result_{compute_content_hash(encoded)[:24]}",),
    )


def _assert_malformed(contract):
    with pytest.raises(ValueError, match="^malformed system fail-method Contract$"):
        _fail_method("worker:adapter-test", contract)


def test_current_compiler_failure_contract_returns_its_declared_output():
    _parent, contract = _current_claimed_fail_contract()
    assert contract.origin == "system"
    assert "plugin:fail" in contract.labels
    assert contract.outputs == (
        f"failure_result_{compute_content_hash(contract.description)[:24]}",
    )

    # This is the unsigned raw Candidate returned by the example Worker owner.
    candidate = CalibrationVerifier("worker:adapter-test").invoke(contract, [])
    action = json.loads(candidate.raw_output.removeprefix("/exec "))
    assert action == {
        "outputs": {
            contract.outputs[0]: json.dumps(
                {"method": "fail", "reason": "source failed safely"},
                sort_keys=True,
            )
        }
    }


def test_adapter_compatibility_slot_requires_matching_system_method():
    _parent, contract = _current_claimed_fail_contract()
    compatibility = replace(contract, outputs=(f"_fail_result_{contract.parent_id}",))

    candidate = CalibrationVerifier("worker:adapter-test").invoke(compatibility, [])
    action = json.loads(candidate.raw_output.removeprefix("/exec "))
    assert tuple(action["outputs"]) == compatibility.outputs


def test_unclaimed_legacy_method_contract_remains_an_adapter_noop():
    parent, _current = _current_claimed_fail_contract()
    legacy = method_contract(
        parent, WorkerAction(type="fail", payload={"reason": "legacy failure"})
    )

    assert "plugin:fail" not in legacy.labels
    assert _fail_method("worker:adapter-test", legacy) is None


def test_fail_adapter_rejects_wrong_origin_or_unrelated_output_slot():
    _parent, contract = _current_claimed_fail_contract()

    _assert_malformed(replace(contract, origin="planner"))
    _assert_malformed(replace(contract, outputs=("failure_result_unrelated",)))
    _assert_malformed(replace(contract, outputs=(*contract.outputs, "extra")))


def test_fail_adapter_requires_nonempty_matching_outer_parent_binding():
    _parent, contract = _current_claimed_fail_contract()
    mismatched = _with_description(
        contract,
        lambda description: description.__setitem__(
            "parent_contract_id", "task:v3:other-parent"
        ),
    )
    _assert_malformed(mismatched)

    def remove_parent(description):
        description.pop("parent_contract_id", None)

    missing_outer_and_contract_parent = replace(
        _with_description(contract, remove_parent), parent_id=None
    )
    _assert_malformed(missing_outer_and_contract_parent)


def test_fail_adapter_rejects_malformed_payload_and_description():
    _parent, contract = _current_claimed_fail_contract()

    blank_reason = _with_description(
        contract,
        lambda description: description["payload"].__setitem__("reason", " "),
    )
    _assert_malformed(blank_reason)

    wrong_method = _with_description(
        contract, lambda description: description.__setitem__("method", "attest")
    )
    _assert_malformed(wrong_method)
    _assert_malformed(replace(contract, description="not-json"))
