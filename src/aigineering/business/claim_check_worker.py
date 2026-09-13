"""Disclosed-Asset adapters for bounded structural claim checks.

These Workers return Candidates only. They neither fetch evidence nor interpret
free-text reasoning, and never attest semantic correctness.
"""

from __future__ import annotations

import json

from aigineering.business.claim_checks import (
    check_evidence_bindings,
    check_original_text,
)
from aigineering.protocol.types import Asset, Candidate, Contract
from aigineering.plugins.task_semantics import method_payload
from aigineering.core.worker_routing import WorkerRegistration

MAX_INPUT_BYTES = 1_048_576


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _read_json(text: str):
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("check input exceeds 1 MiB")
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=lambda value: _invalid_constant(value),
    )


def _invalid_constant(value):
    raise ValueError("non-finite JSON constant")


def _available(asset: Asset) -> bool:
    return (
        asset.promptable
        and not asset.tombstoned
        and asset.disclosure_view == "original"
    )


def _method_inputs(contract, assets):
    """Consume the exact binding emitted by the existing method adapter."""
    binding = _read_json(contract.description.splitlines()[-1])
    if not isinstance(binding, dict) or binding.get("schema") != "method-invocation-v1":
        raise ValueError("expected a method-invocation-v1 binding")
    inputs = binding.get("inputs")
    outputs = binding.get("outputs")
    if not isinstance(inputs, dict) or set(inputs) != {"source", "report"}:
        raise ValueError("expected exact source/report method slots")
    if not isinstance(outputs, dict) or set(outputs) != {"assessment"}:
        raise ValueError("expected the assessment method output slot")
    if tuple(contract.outputs) != (outputs["assessment"],):
        raise ValueError("method output binding differs from Contract")
    by_id = {asset.id: asset for asset in assets}
    if len(by_id) != len(assets):
        raise ValueError("ambiguous disclosed Asset IDs")
    method_id = binding.get("method_asset_id")
    required = [method_id, *inputs.values()]
    if any(
        not isinstance(asset_id, str)
        or asset_id not in contract.context_asset_ids
        or asset_id not in by_id
        or not _available(by_id[asset_id])
        for asset_id in required
    ):
        raise ValueError("method binding requires exact available context Assets")
    return by_id[inputs["source"]], by_id[inputs["report"]]


def _gate_inputs(contract, assets):
    """Select only unambiguous, declared business inputs, never prompt text."""
    selected = []
    for name in ("research_result", "review_report"):
        matches = [asset for asset in assets if asset.name == name]
        if (
            name not in contract.inputs
            or len(matches) != 1
            or not _available(matches[0])
        ):
            raise ValueError(
                "gate requires unambiguous original source and report inputs"
            )
        selected.append(matches[0])
    return tuple(selected)


def _candidate(worker_id, action, payload):
    return Candidate(
        worker_id=worker_id,
        raw_output=f"/{action} "
        + json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


class ClaimCheckWorker:
    """One fixed checker, selected by trusted operator configuration."""

    def __init__(self, worker_id: str, checker=None, *, gate=False):
        self.worker_id = worker_id
        self._checker = checker
        self._gate = gate

    def registration(self):
        capability = (
            "claim.check.gate"
            if self._gate
            else {
                check_original_text: "claim.check.original-text",
                check_evidence_bindings: "claim.check.evidence-bindings",
            }.get(self._checker, "claim.check")
        )
        return WorkerRegistration(
            self.worker_id,
            capabilities=(capability,),
            pools=("validation",),
            profile_id="claim-validation-v1",
        )

    def invoke(self, contract: Contract, disclosed_assets: list[Asset]) -> Candidate:
        try:
            if len(contract.outputs) != 1:
                raise ValueError("claim checks require exactly one output")
            # /fail publishes ordinary failure-report work. Complete that work
            # rather than recursively requesting another failure task.
            failure = method_payload(contract)
            if contract.origin == "system" and failure.get("method") == "fail":
                return _candidate(
                    self.worker_id,
                    "exec",
                    {
                        "outputs": {
                            contract.outputs[0]: json.dumps(failure, sort_keys=True)
                        }
                    },
                )
            source, report = (
                _gate_inputs(contract, disclosed_assets)
                if self._gate
                else _method_inputs(contract, disclosed_assets)
            )
            source_data, report_data = (
                _read_json(source.content),
                _read_json(report.content),
            )
            if self._gate:
                checks = [
                    check_original_text(source_data, report_data),
                    check_evidence_bindings(source_data, report_data),
                ]
                failures = [item["findings"] for item in checks if not item["passed"]]
                if failures:
                    return _candidate(
                        self.worker_id,
                        "fail",
                        {
                            "reason": "structural claim check failed: "
                            + "\n".join(failures)
                        },
                    )
                content = json.dumps(
                    {
                        "source_asset_id": source.id,
                        "report_asset_id": report.id,
                        "structural_checks_passed": True,
                        "semantic_checked": False,
                    },
                    sort_keys=True,
                )
            else:
                content = json.dumps(
                    self._checker(source_data, report_data), sort_keys=True
                )
            return _candidate(
                self.worker_id, "exec", {"outputs": {contract.outputs[0]: content}}
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            RecursionError,
            UnicodeError,
        ):
            return _candidate(
                self.worker_id,
                "fail",
                {"reason": "claim check input or exact method binding is invalid"},
            )


def create_original_text_worker(*, worker_id: str):
    return ClaimCheckWorker(worker_id, check_original_text)


def create_evidence_bindings_worker(*, worker_id: str):
    return ClaimCheckWorker(worker_id, check_evidence_bindings)


def create_structural_gate_worker(*, worker_id: str):
    """Fail closed on structural violations; success is not semantic attestation."""
    return ClaimCheckWorker(worker_id, gate=True)
