"""Scripted malformed and resource-bound calibration CLI Workers."""

from __future__ import annotations

import json

from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.types import Candidate
from fixtures.thermal_calibration_workers import (
    CalibrationProtocolWorker,
    build_verifier_worker as build_protocol_verifier,
)


class MalformedRootWorker(CalibrationProtocolWorker):
    def invoke(self, contract, disclosed_assets):
        if contract.origin == "human" and contract.outputs == ("calibration_report",):
            return Candidate(worker_id=self.worker_id, raw_output="/tool {not-json")
        return super().invoke(contract, disclosed_assets)


class MissingCopiedObservationWorker(CalibrationProtocolWorker):
    def _publish(self, contract, disclosed_assets, successful):
        candidate = super()._publish(contract, disclosed_assets, successful)
        action = json.loads(candidate.raw_output.split(" ", 1)[1])
        outputs = action["outputs"]
        evidence = json.loads(outputs["calibration_evidence"])
        evidence["observations"].pop()
        evidence["observation_asset_ids"].pop()
        outputs["calibration_evidence"] = json.dumps(evidence, sort_keys=True)
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec " + json.dumps({"outputs": outputs}, sort_keys=True),
        )


class TwoUnitPlanner(CalibrationProtocolWorker):
    def invoke(self, contract, disclosed_assets):
        candidate = super().invoke(contract, disclosed_assets)
        if "plugin:plan.compile" not in contract.labels:
            return candidate
        action = json.loads(candidate.raw_output.split(" ", 1)[1])
        blueprint = json.loads(action["outputs"]["planning_blueprint"])
        research = next(
            item
            for item in blueprint["contracts"]
            if item["name"] == "calibration_research"
        )
        research["budget"] = 2
        action["outputs"]["planning_blueprint"] = json.dumps(blueprint, sort_keys=True)
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec " + json.dumps(action, sort_keys=True),
        )


class VerifierFailPlanner(CalibrationProtocolWorker):
    def invoke(self, contract, disclosed_assets):
        candidate = super().invoke(contract, disclosed_assets)
        if "plugin:plan.compile" not in contract.labels:
            return candidate
        action = json.loads(candidate.raw_output.split(" ", 1)[1])
        blueprint = json.loads(action["outputs"]["planning_blueprint"])
        verifier = next(
            item
            for item in blueprint["contracts"]
            if item["name"] == "calibration_verifier"
        )
        # The verifier must have one allowance unit left to publish its chosen
        # /fail method result; budget 1 is consumed by the verifier claim.
        verifier["budget"] = 2
        action["outputs"]["planning_blueprint"] = json.dumps(blueprint, sort_keys=True)
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec " + json.dumps(action, sort_keys=True),
        )


class FailureBoundaryVerifier:
    """Keep the production verifier and complete its ordinary /fail method."""

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self._verifier = build_protocol_verifier(worker_id=worker_id)

    def invoke(self, contract, disclosed_assets):
        failed = _complete_fail_method(self.worker_id, contract)
        if failed is not None:
            return failed
        return self._verifier.invoke(contract, disclosed_assets)


def _complete_fail_method(worker_id, contract):
    """Return the declared failure result for either canonical method shape."""
    if contract.origin != "system" or "plugin:fail" not in contract.labels:
        return None
    payload = method_payload(contract)
    action = payload.get("payload", {})
    if (
        payload.get("method") != "fail"
        or len(contract.outputs) != 1
        or not contract.outputs[0].startswith(("_fail_result_", "failure_result_"))
        or not isinstance(action, dict)
        or not isinstance(action.get("reason"), str)
        or not action["reason"].strip()
    ):
        raise ValueError("malformed system fail-method Contract")
    return Candidate(
        worker_id=worker_id,
        raw_output="/exec "
        + json.dumps(
            {
                "outputs": {
                    contract.outputs[0]: json.dumps(
                        {"method": "fail", "reason": action["reason"]},
                        sort_keys=True,
                    )
                }
            },
            sort_keys=True,
        ),
    )


def build_malformed_root_worker(*, worker_id: str):
    return MalformedRootWorker(worker_id)


def build_missing_observation_worker(*, worker_id: str):
    return MissingCopiedObservationWorker(worker_id)


def build_two_unit_planner(*, worker_id: str):
    return TwoUnitPlanner(worker_id)


def build_verifier_fail_planner(*, worker_id: str):
    return VerifierFailPlanner(worker_id)


def build_failure_boundary_verifier(*, worker_id: str):
    return FailureBoundaryVerifier(worker_id)
