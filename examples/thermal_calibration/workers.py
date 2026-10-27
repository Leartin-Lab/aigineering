"""Configured local verifier for the synthetic calibration CLI example."""

from __future__ import annotations

import json

from aigineering.core.ids import compute_content_hash
from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.types import Asset, Candidate, Contract

from examples.thermal_calibration.oracle import evaluate_report

SKILL_LABEL = "_skill_content_thermal_calibration"
TOOL_NAMES = ("normalize_calibration", "analyze_calibration")
NORMALIZED_KEYS = {
    "schema",
    "case",
    "standard",
    "case_sha256",
    "standard_sha256",
    "samples",
}


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_loads(value):
    return json.loads(
        value,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda constant: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant: {constant}")
        ),
    )


def _strict_equal(left, right) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _strict_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _candidate(worker_id: str, action: str, payload: dict) -> Candidate:
    return Candidate(
        worker_id=worker_id,
        raw_output=f"/{action} " + json.dumps(payload, sort_keys=True),
    )


def _exec(worker_id: str, outputs: dict[str, str]) -> Candidate:
    return _candidate(worker_id, "exec", {"outputs": outputs})


def _asset(assets: list[Asset], name: str) -> tuple[Asset, object]:
    matches = [item for item in assets if item.name == name]
    if len(matches) != 1:
        raise ValueError(f"expected one disclosed {name} asset")
    asset = matches[0]
    try:
        content = _strict_json_loads(asset.content)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"disclosed {name} asset is not JSON") from exc
    return asset, content


def _observation_assets(assets: list[Asset]) -> list[tuple[Asset, dict]]:
    observations = []
    for asset in assets:
        if not asset.name.startswith(("_tool_obs_", "tool_observation_")):
            continue
        try:
            value = json.loads(asset.content)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("tool") in TOOL_NAMES:
            observations.append((asset, value))
    return observations


def _fail_method(worker_id: str, contract: Contract) -> Candidate | None:
    if "plugin:fail" not in contract.labels:
        return None
    if not isinstance(contract.description, str):
        raise ValueError("malformed system fail-method Contract")
    description = method_payload(contract)
    payload = description.get("payload", {})
    parent_contract_id = description.get("parent_contract_id")
    if (
        contract.origin != "system"
        or description.get("method") != "fail"
        or not isinstance(parent_contract_id, str)
        or not parent_contract_id.strip()
        or not isinstance(contract.parent_id, str)
        or not contract.parent_id.strip()
        or parent_contract_id != contract.parent_id
        or len(contract.outputs) != 1
        or not isinstance(payload, dict)
        or not isinstance(payload.get("reason"), str)
        or not payload["reason"].strip()
    ):
        raise ValueError("malformed system fail-method Contract")
    current_output = f"failure_result_{compute_content_hash(contract.description)[:24]}"
    compatibility_output = f"_fail_result_{parent_contract_id}"
    if contract.outputs[0] not in (current_output, compatibility_output):
        raise ValueError("malformed system fail-method Contract")
    return _exec(
        worker_id,
        {
            contract.outputs[0]: json.dumps(
                {"method": "fail", "reason": payload["reason"]},
                sort_keys=True,
            )
        },
    )


class CalibrationVerifier:
    """Independently check the exact report and its copied tool evidence."""

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def invoke(self, contract: Contract, disclosed_assets: list[Asset]) -> Candidate:
        failed = _fail_method(self.worker_id, contract)
        if failed is not None:
            return failed
        try:
            return self._attest(contract, disclosed_assets)
        except (KeyError, TypeError, ValueError):
            return _candidate(
                self.worker_id, "fail", {"reason": "calibration evidence check failed"}
            )

    def _attest(self, contract: Contract, disclosed_assets: list[Asset]) -> Candidate:
        if contract.outputs != ("calibration_receipt",):
            raise ValueError("verifier must declare only its receipt")
        required_inputs = {
            "calibration_case",
            "calibration_standard",
            "calibration_report",
            "calibration_evidence",
        }
        if len(contract.inputs) != 4 or set(contract.inputs) != required_inputs:
            raise ValueError("verifier input roles are incomplete")
        if contract.worker_capabilities != ("calibration.verify",):
            raise ValueError("verifier capability must be exact")
        if contract.worker_pools != ("verification",):
            raise ValueError("verifier pool must be exact")
        description = _strict_json_loads(contract.description)
        required_description = {
            "schema",
            "root_contract_id",
            "calibration_case_asset_id",
            "calibration_standard_asset_id",
            "policy_version",
        }
        if set(description) != required_description:
            raise ValueError("verifier target description is incomplete")

        case_asset, case = _asset(disclosed_assets, "calibration_case")
        standard_asset, standard = _asset(disclosed_assets, "calibration_standard")
        report_asset, report = _asset(disclosed_assets, "calibration_report")
        _evidence_asset, evidence = _asset(disclosed_assets, "calibration_evidence")
        if description != {
            "schema": "thermal-calibration-verifier-target-v1",
            "root_contract_id": description["root_contract_id"],
            "calibration_case_asset_id": case_asset.id,
            "calibration_standard_asset_id": standard_asset.id,
            "policy_version": "synthetic-temperature-calibration-v1",
        }:
            raise ValueError("verifier target does not bind exact disclosed inputs")
        if (
            not isinstance(description["root_contract_id"], str)
            or not description["root_contract_id"]
        ):
            raise ValueError("verifier root target is missing")
        if not isinstance(evidence, dict) or set(evidence) != {
            "schema",
            "calibration_case_asset_id",
            "calibration_standard_asset_id",
            "observation_asset_ids",
            "observations",
        }:
            raise ValueError("business evidence has an unexpected shape")
        observations = evidence["observations"]
        observation_ids = evidence["observation_asset_ids"]
        if (
            evidence["schema"] != "thermal-calibration-evidence-v1"
            or evidence["calibration_case_asset_id"] != case_asset.id
            or evidence["calibration_standard_asset_id"] != standard_asset.id
            or not isinstance(observations, list)
            or len(observations) != 2
            or not isinstance(observation_ids, list)
            or len(observation_ids) != 2
            or any(not isinstance(item, str) or not item for item in observation_ids)
            or len(set(observation_ids)) != 2
        ):
            raise ValueError("business evidence does not bind the two exact inputs")
        copied = []
        for item in observations:
            if not isinstance(item, dict) or set(item) != {"asset_id", "observation"}:
                raise ValueError("copied observation has an unexpected shape")
            copied.append(item)
        if [item["asset_id"] for item in copied] != observation_ids:
            raise ValueError("copied observation IDs do not match their list")
        by_tool = {}
        ordered_tools = []
        for item in copied:
            observation = item["observation"]
            if (
                not isinstance(observation, dict)
                or set(observation)
                != {"ok", "tool", "result", "error", "error_type", "retryable"}
                or observation.get("ok") is not True
                or observation.get("tool") not in TOOL_NAMES
                or not isinstance(observation.get("result"), str)
                or observation.get("error") != ""
                or observation.get("error_type") != ""
                or observation.get("retryable") is not False
            ):
                raise ValueError("copied observation is not a successful tool result")
            if observation["tool"] in by_tool:
                raise ValueError("duplicate tool observation")
            by_tool[observation["tool"]] = observation
            ordered_tools.append(observation["tool"])
        if set(by_tool) != set(TOOL_NAMES):
            raise ValueError("both distinct tool observations are required")
        if tuple(ordered_tools) != TOOL_NAMES:
            raise ValueError("tool observations are not in declared call order")
        normalized = _strict_json_loads(by_tool["normalize_calibration"]["result"])
        analyzed = _strict_json_loads(by_tool["analyze_calibration"]["result"])
        if (
            not isinstance(normalized, dict)
            or not isinstance(analyzed, dict)
            or not isinstance(report, dict)
            or set(normalized) != NORMALIZED_KEYS
            or normalized.get("schema") != "thermal-calibration-normalized-v1"
            or not _strict_equal(normalized.get("case"), case)
            or not _strict_equal(normalized.get("standard"), standard)
            or not _strict_equal(
                normalized.get("case_sha256"), report.get("case_sha256")
            )
            or not _strict_equal(
                normalized.get("standard_sha256"), report.get("standard_sha256")
            )
            or not _strict_equal(normalized.get("samples"), report.get("samples"))
            or not _strict_equal(analyzed, report)
        ):
            raise ValueError("observations do not match the exact disclosed sources")
        result = evaluate_report(
            case_asset.content, standard_asset.content, report_asset.content
        )
        if result.get("passed") is not True:
            raise ValueError("independent Fraction oracle rejected the report")
        output = {
            "contract_id": description["root_contract_id"],
            "output_name": "calibration_report",
            "asset_id": report_asset.id,
            "verdict": "accepted",
            "evidence_asset_ids": sorted((case_asset.id, standard_asset.id)),
            "outputs": {
                "calibration_receipt": json.dumps(
                    {
                        "case_asset_id": case_asset.id,
                        "standard_asset_id": standard_asset.id,
                        "report_asset_id": report_asset.id,
                        "oracle_passed": True,
                    },
                    sort_keys=True,
                )
            },
        }
        return _candidate(self.worker_id, "attest", output)


def build_verifier_worker(*, worker_id: str) -> CalibrationVerifier:
    return CalibrationVerifier(worker_id)
