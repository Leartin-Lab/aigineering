"""Deterministic local Workers for the configured CLI research example."""

from __future__ import annotations

import json
import math

from aigineering.protocol.types import Candidate
from aigineering.plugins.task_semantics import method_payload


def _asset_json(assets, name: str) -> tuple[object, dict]:
    matches = [asset for asset in assets if asset.name == name]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one disclosed {name!r} Asset, got {len(matches)}"
        )
    asset = matches[0]
    return asset, json.loads(asset.content)


def _action(worker_id: str, raw: str) -> Candidate:
    return Candidate(worker_id=worker_id, raw_output=raw)


def _exec(worker_id: str, outputs: dict[str, str]) -> Candidate:
    return _action(
        worker_id,
        "/exec " + json.dumps({"outputs": outputs}, sort_keys=True),
    )


def _complete_fail_method(worker_id: str, contract) -> Candidate | None:
    """Finish only the declared system fail method with its sole result output."""
    if "plugin:fail" not in contract.labels:
        return None
    method = method_payload(contract)
    payload = method.get("payload", {})
    if (
        method.get("method") != "fail"
        or len(contract.outputs) != 1
        or not contract.outputs[0].startswith("_fail_result_")
        or not isinstance(payload, dict)
        or not isinstance(payload.get("reason"), str)
        or not payload["reason"].strip()
    ):
        raise AssertionError("malformed system fail-method Contract")
    return _exec(
        worker_id,
        {
            contract.outputs[0]: json.dumps(
                {"method": "fail", "reason": payload["reason"]}, sort_keys=True
            )
        },
    )


def _observation_assets(assets) -> list[tuple[object, dict]]:
    found = []
    for asset in assets:
        if asset.origin not in {
            "tool",
            "tool_observation",
        } and not asset.name.startswith(("_tool_obs_", "tool_observation_")):
            continue
        try:
            observation = json.loads(asset.content)
        except (TypeError, ValueError):
            continue
        if isinstance(observation, dict) and observation.get("tool") in {
            "summarize_sample",
            "convert_mean",
        }:
            found.append((asset, observation))
    return found


class ResearchWorker:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def invoke(self, contract, disclosed_assets):
        labels = set(contract.labels)
        fail_result = _complete_fail_method(self.worker_id, contract)
        if fail_result is not None:
            return fail_result
        if (
            contract.origin == "human"
            and "_skill_content_cli_research" in labels
            and contract.outputs == ("measurement_report",)
        ):
            return _action(
                self.worker_id,
                '/plan {"reason":"calculate the sample using committed tool evidence, then independently verify the report"}',
            )
        if "plugin:plan.draft" in labels:
            return _exec(
                self.worker_id,
                {
                    contract.outputs[0]: json.dumps(
                        {
                            "goals": [
                                "report the synthetic sample mean in Celsius and Fahrenheit"
                            ],
                            "evidence_needs": [
                                "sample",
                                "both committed tool observations",
                            ],
                            "uncertainties": [
                                "sample is synthetic and not population evidence"
                            ],
                            "proposed_steps": [
                                "calculate with scoped tools",
                                "independently verify",
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
                            "producers": ["research", "verifier"],
                            "consumers": [
                                "research consumes sample",
                                "verifier consumes sample, report and evidence",
                            ],
                            "missing_inputs": [],
                            "cycles": [],
                            "parallel_groups": [],
                            "capability_needs": [
                                "measurement.research",
                                "measurement.verify",
                            ],
                            "authority_risks": [],
                            "allowance_needs": [
                                "research needs two tool calls, continuations, and bounded recovery"
                            ],
                        },
                        sort_keys=True,
                    )
                },
            )
        if "plugin:plan.compile" in labels:
            sample, _sample_payload = _asset_json(disclosed_assets, "sample")
            blueprint = {
                "contracts": [
                    {
                        "name": "measurement_research",
                        "description": "Use the sample and both committed tool observations to produce the report and evidence.",
                        "inputs": ["sample"],
                        "outputs": ["measurement_report", "measurement_evidence"],
                        "activation": "sample",
                        "budget": 12,
                        "tool_scope": ["summarize_sample", "convert_mean"],
                        "labels": ["_skill_content_cli_research"],
                        "capability_needs": ["measurement.research"],
                        "pool_needs": ["research"],
                        "delegation_capabilities": [],
                        "delegation_pools": [],
                    },
                    {
                        "name": "measurement_verifier",
                        "description": json.dumps(
                            {
                                "task": "Recompute the report from the exact sample and compare both complete tool observations in the business evidence. Attest only after every check succeeds.",
                                "parent_contract_id": contract.parent_id,
                                "sample_asset_id": sample.id,
                                "policy": "measurement-research-v1",
                            },
                            sort_keys=True,
                        ),
                        "inputs": [
                            "sample",
                            "measurement_report",
                            "measurement_evidence",
                        ],
                        "outputs": ["verification_receipt"],
                        "activation": "sample AND measurement_report AND measurement_evidence",
                        "budget": 1,
                        "tool_scope": [],
                        "labels": [],
                        "capability_needs": ["measurement.verify"],
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

        if contract.tool_scope:
            if "summarize_sample" in contract.tool_scope:
                _sample, sample_payload = _asset_json(disclosed_assets, "sample")
                return _action(
                    self.worker_id,
                    "/tool "
                    + json.dumps(
                        {
                            "name": "summarize_sample",
                            "args": {"values": sample_payload["values"]},
                        },
                        sort_keys=True,
                    ),
                )
            if "convert_mean" in contract.tool_scope:
                observations = _observation_assets(disclosed_assets)
                summaries = [
                    item
                    for item in observations
                    if item[1].get("tool") == "summarize_sample"
                    and item[1].get("ok") is True
                ]
                if len(summaries) != 1:
                    return _action(
                        self.worker_id,
                        "/fail "
                        + json.dumps(
                            {"reason": "missing the committed summary observation"}
                        ),
                    )
                result = summaries[0][1].get("result")
                if not isinstance(result, str):
                    return _action(
                        self.worker_id,
                        '/fail {"reason":"summary result is not JSON text"}',
                    )
                try:
                    summary = json.loads(result)
                except ValueError:
                    return _action(
                        self.worker_id, '/fail {"reason":"summary result is malformed"}'
                    )
                return _action(
                    self.worker_id,
                    "/tool "
                    + json.dumps(
                        {"name": "convert_mean", "args": {"summary": summary}},
                        sort_keys=True,
                    ),
                )
            return _action(
                self.worker_id, '/fail {"reason":"unexpected remaining tool scope"}'
            )

        return self._publish_measurements(contract, disclosed_assets)

    def _publish_measurements(self, contract, disclosed_assets):
        sample, sample_payload = _asset_json(disclosed_assets, "sample")
        observations = _observation_assets(disclosed_assets)
        summary_rows = [
            row for row in observations if row[1].get("tool") == "summarize_sample"
        ]
        conversion_rows = [
            row for row in observations if row[1].get("tool") == "convert_mean"
        ]
        if len(summary_rows) != 1 or len(conversion_rows) != 1:
            return _action(
                self.worker_id,
                "/fail "
                + json.dumps(
                    {"reason": "both complete tool observations are required"}
                ),
            )
        summary_asset, summary_observation = summary_rows[0]
        conversion_asset, conversion_observation = conversion_rows[0]
        if (
            summary_observation.get("ok") is not True
            or conversion_observation.get("ok") is not True
        ):
            return _action(self.worker_id, '/fail {"reason":"tool observation failed"}')
        try:
            summary = json.loads(summary_observation["result"])
            conversion = json.loads(conversion_observation["result"])
        except (KeyError, TypeError, ValueError):
            return _action(
                self.worker_id,
                '/fail {"reason":"tool observation result is malformed"}',
            )
        values = sample_payload["values"]
        mean_c = math.fsum(values) / len(values)
        if summary.get("count") != len(values) or not math.isclose(
            summary.get("mean_c", float("nan")), mean_c, rel_tol=1e-12, abs_tol=1e-12
        ):
            return _action(
                self.worker_id, '/fail {"reason":"summary does not match the sample"}'
            )
        report = {
            "dataset": sample_payload["dataset"],
            "sample_asset_id": sample.id,
            "count": len(values),
            "mean_c": mean_c,
            "mean_f": conversion["mean_f"],
            "observation_asset_ids": [summary_asset.id, conversion_asset.id],
            "limitations": sample_payload["limitations"],
        }
        evidence = {
            "sample_asset_id": sample.id,
            "observations": [
                {"asset_id": summary_asset.id, "observation": summary_observation},
                {
                    "asset_id": conversion_asset.id,
                    "observation": conversion_observation,
                },
            ],
        }
        return _exec(
            self.worker_id,
            {
                "measurement_report": json.dumps(report, sort_keys=True),
                "measurement_evidence": json.dumps(evidence, sort_keys=True),
            },
        )


class VerifierWorker:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def invoke(self, contract, disclosed_assets):
        fail_result = _complete_fail_method(self.worker_id, contract)
        if fail_result is not None:
            return fail_result
        sample, sample_payload = _asset_json(disclosed_assets, "sample")
        report_asset, report = _asset_json(disclosed_assets, "measurement_report")
        _evidence_asset, evidence = _asset_json(
            disclosed_assets, "measurement_evidence"
        )
        try:
            description = json.loads(contract.description)
        except ValueError:
            description = {}
        values = sample_payload["values"]
        expected_c = math.fsum(values) / len(values)
        expected_f = expected_c * 9 / 5 + 32
        observations = evidence.get("observations", [])
        indexed = {
            item.get("asset_id"): item.get("observation")
            for item in observations
            if isinstance(item, dict)
        }
        summary_obs = next(
            (
                item
                for item in indexed.values()
                if item.get("tool") == "summarize_sample"
            ),
            {},
        )
        conversion_obs = next(
            (item for item in indexed.values() if item.get("tool") == "convert_mean"),
            {},
        )
        try:
            summary_result = json.loads(summary_obs["result"])
            conversion_result = json.loads(conversion_obs["result"])
        except (KeyError, TypeError, ValueError):
            return _action(
                self.worker_id, '/fail {"reason":"evidence observations are malformed"}'
            )
        checks_ok = (
            contract.outputs == ("verification_receipt",)
            and set(contract.inputs)
            == {"sample", "measurement_report", "measurement_evidence"}
            and isinstance(description.get("parent_contract_id"), str)
            and bool(description.get("parent_contract_id", "").strip())
            and description.get("sample_asset_id") == sample.id
            and description.get("policy") == "measurement-research-v1"
            and evidence.get("sample_asset_id") == sample.id
            and len(observations) == 2
            and len(indexed) == 2
            and report.get("dataset") == sample_payload.get("dataset")
            and report.get("sample_asset_id") == sample.id
            and report.get("count") == len(values)
            and math.isclose(
                report.get("mean_c", float("nan")),
                expected_c,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            and math.isclose(
                report.get("mean_f", float("nan")),
                expected_f,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            and report.get("limitations") == sample_payload.get("limitations")
            and report.get("observation_asset_ids") == list(indexed)
            and summary_result.get("count") == len(values)
            and summary_result.get("success") is True
            and summary_result.get("total_c") == math.fsum(values)
            and math.isclose(
                summary_result.get("mean_c", float("nan")),
                expected_c,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            and conversion_result.get("digest") == summary_result.get("digest")
            and conversion_result.get("success") is True
            and math.isclose(
                conversion_result.get("mean_f", float("nan")),
                expected_f,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        )
        if not checks_ok:
            return _action(
                self.worker_id,
                '/fail {"reason":"independent measurement checks failed"}',
            )
        output = {
            "contract_id": description["parent_contract_id"],
            "output_name": "measurement_report",
            "asset_id": report_asset.id,
            "verdict": "accepted",
            "evidence_asset_ids": [sample.id],
            "outputs": {
                "verification_receipt": json.dumps(
                    {"sample_asset_id": sample.id, "checks": "passed"}, sort_keys=True
                )
            },
        }
        return _action(self.worker_id, "/attest " + json.dumps(output, sort_keys=True))


def build_research_worker(*, worker_id: str) -> ResearchWorker:
    return ResearchWorker(worker_id)


def build_verifier_worker(*, worker_id: str) -> VerifierWorker:
    return VerifierWorker(worker_id)
