"""Deterministic protocol Workers for configured calibration CLI tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

from aigineering.core.tools import ToolRegistry

from examples.thermal_calibration.tools import build_registry
from examples.thermal_calibration.workers import (
    TOOL_NAMES,
    _asset,
    _candidate,
    _exec,
    _fail_method,
    _observation_assets,
    build_verifier_worker as production_build_verifier_worker,
)

SKILL_LABEL = "_skill_content_thermal_calibration"


class CalibrationProtocolWorker:
    """Fixed protocol participant used only by the integration test fleet."""

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def invoke(self, contract, disclosed_assets):
        failed = _fail_method(self.worker_id, contract)
        if failed is not None:
            return failed
        labels = set(contract.labels)
        if (
            contract.origin == "human"
            and SKILL_LABEL in labels
            and contract.outputs == ("calibration_report",)
        ):
            return _candidate(
                self.worker_id,
                "plan",
                {"reason": "derive the report using both scoped tools, then verify it"},
            )
        if "plugin:plan.draft" in labels:
            return _exec(
                self.worker_id,
                {
                    contract.outputs[0]: json.dumps(
                        {
                            "goals": [
                                "compute per-sample calibration error and drift",
                                "independently verify the report and source evidence",
                            ],
                            "evidence_needs": [
                                "the exact case and standard",
                                "both complete tool observations",
                            ],
                            "uncertainties": [
                                "all measurements are synthetic and do not establish real-world calibration"
                            ],
                            "proposed_steps": [
                                "normalize source values",
                                "analyze the normalized source",
                                "independently evaluate and attest the report",
                            ],
                        },
                        sort_keys=True,
                    )
                },
            )
        if "plugin:plan.dependencies" in labels:
            return _exec(
                self.worker_id,
                {
                    contract.outputs[0]: json.dumps(
                        {
                            "producers": [
                                "calibration research",
                                "independent verifier",
                            ],
                            "consumers": [
                                "research consumes both source assets",
                                "verifier consumes sources, report, and evidence",
                            ],
                            "missing_inputs": [],
                            "cycles": [],
                            "parallel_groups": [],
                            "capability_needs": [
                                "calibration.research",
                                "calibration.verify",
                            ],
                            "authority_risks": [],
                            "allowance_needs": [
                                "research requires two tool calls and continuations"
                            ],
                        },
                        sort_keys=True,
                    )
                },
            )
        if "plugin:plan.compile" in labels:
            case, _case_value = _asset(disclosed_assets, "calibration_case")
            standard, _standard_value = _asset(disclosed_assets, "calibration_standard")
            blueprint = {
                "contracts": [
                    {
                        "name": "calibration_research",
                        "description": "Use the exact calibration inputs and both committed tool observations to produce the report and evidence.",
                        "inputs": ["calibration_case", "calibration_standard"],
                        "outputs": ["calibration_report", "calibration_evidence"],
                        "activation": "calibration_case AND calibration_standard",
                        "budget": 16,
                        "tool_scope": list(TOOL_NAMES),
                        "labels": [SKILL_LABEL],
                        "capability_needs": ["calibration.research"],
                        "pool_needs": ["research"],
                        "delegation_capabilities": [],
                        "delegation_pools": [],
                    },
                    {
                        "name": "calibration_verifier",
                        "description": json.dumps(
                            {
                                "schema": "thermal-calibration-verifier-target-v1",
                                "root_contract_id": contract.parent_id,
                                "calibration_case_asset_id": case.id,
                                "calibration_standard_asset_id": standard.id,
                                "policy_version": "synthetic-temperature-calibration-v1",
                            },
                            sort_keys=True,
                        ),
                        "inputs": [
                            "calibration_case",
                            "calibration_standard",
                            "calibration_report",
                            "calibration_evidence",
                        ],
                        "outputs": ["calibration_receipt"],
                        "activation": "calibration_case AND calibration_standard AND calibration_report AND calibration_evidence",
                        "budget": 1,
                        "tool_scope": [],
                        "labels": [SKILL_LABEL],
                        "capability_needs": ["calibration.verify"],
                        "pool_needs": ["verification"],
                        "delegation_capabilities": [],
                        "delegation_pools": [],
                    },
                ]
            }
            return _exec(
                self.worker_id,
                {"planning_blueprint": json.dumps(blueprint, sort_keys=True)},
            )

        observations = _observation_assets(disclosed_assets)
        successful = {
            item[1]["tool"]: item[1]
            for item in observations
            if item[1].get("ok") is True
        }
        # A continuation may have consumed all tool_scope already. Published
        # observations are durable inputs; use them before inspecting remaining
        # authority so the final ordinary exec does not fail after the last tool.
        if set(successful) == set(TOOL_NAMES):
            return self._publish(contract, disclosed_assets, successful)
        if "normalize_calibration" not in successful:
            if "normalize_calibration" not in contract.tool_scope:
                return _candidate(
                    self.worker_id,
                    "fail",
                    {"reason": "normalization observation is missing"},
                )
            _case, case_value = _asset(disclosed_assets, "calibration_case")
            _standard, standard_value = _asset(disclosed_assets, "calibration_standard")
            return _candidate(
                self.worker_id,
                "tool",
                {
                    "name": "normalize_calibration",
                    "args": {"case": case_value, "standard": standard_value},
                },
            )
        if "analyze_calibration" not in successful:
            if "analyze_calibration" not in contract.tool_scope:
                return _candidate(
                    self.worker_id,
                    "fail",
                    {"reason": "analysis observation is missing"},
                )
            normalized_text = successful["normalize_calibration"].get("result")
            if not isinstance(normalized_text, str):
                return _candidate(
                    self.worker_id,
                    "fail",
                    {"reason": "normalization result is malformed"},
                )
            normalized = json.loads(normalized_text)
            return _candidate(
                self.worker_id,
                "tool",
                {"name": "analyze_calibration", "args": {"normalized": normalized}},
            )
        return _candidate(
            self.worker_id, "fail", {"reason": "no authorized work remains"}
        )

    def _publish(self, contract, disclosed_assets, successful):
        case_asset, _case = _asset(disclosed_assets, "calibration_case")
        standard_asset, _standard = _asset(disclosed_assets, "calibration_standard")
        observations = [
            (asset, observation)
            for asset, observation in _observation_assets(disclosed_assets)
            if observation.get("ok") is True
        ]
        by_tool = {
            observation["tool"]: (asset, observation)
            for asset, observation in observations
        }
        if (
            len(observations) != 2
            or set(by_tool) != set(TOOL_NAMES)
            or len({row[0].id for row in by_tool.values()}) != 2
        ):
            return _candidate(
                self.worker_id,
                "fail",
                {"reason": "both successful tool observations are required"},
            )
        report_text = successful["analyze_calibration"].get("result")
        if not isinstance(report_text, str):
            return _candidate(
                self.worker_id, "fail", {"reason": "analysis result is malformed"}
            )
        report = json.loads(report_text)
        evidence = {
            "schema": "thermal-calibration-evidence-v1",
            "calibration_case_asset_id": case_asset.id,
            "calibration_standard_asset_id": standard_asset.id,
            "observation_asset_ids": [by_tool[name][0].id for name in TOOL_NAMES],
            "observations": [
                {"asset_id": by_tool[name][0].id, "observation": by_tool[name][1]}
                for name in TOOL_NAMES
            ],
        }
        return _exec(
            self.worker_id,
            {
                "calibration_report": json.dumps(report, sort_keys=True),
                "calibration_evidence": json.dumps(evidence, sort_keys=True),
            },
        )


def build_calibration_worker(*, worker_id: str) -> CalibrationProtocolWorker:
    return CalibrationProtocolWorker(worker_id)


def build_verifier_worker(*, worker_id: str):
    return production_build_verifier_worker(worker_id=worker_id)


def build_failure_registry():
    """Fail the first normalize output schema check, then run tools normally."""
    source = build_registry()
    registry = ToolRegistry()
    marker_value = os.environ.get("AIG_CALIBRATION_FAIL_FIRST_TOOL_MARKER")
    marker = Path(marker_value) if marker_value else None
    for spec in source.list_specs():

        def handler(args, *, tool_name=spec.name):
            if tool_name == "normalize_calibration" and marker is not None:
                try:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(
                        marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                except FileExistsError:
                    pass
                else:
                    os.close(descriptor)
                    return '{"schema":"injected-invalid-normalized-v1"}'
            return source.run(tool_name, args)

        registry.register(spec, handler)
    return registry
