"""Stateless Workers for a single-generation claim review workflow.

Each model-backed invocation makes one ordinary LLMWorker invocation. Plans,
claims, continuations and retries remain visible runtime work.
"""

import json
import os
from dataclasses import replace

from aigineering.agent.llm import LLMWorker
from aigineering.agent.worker import WorkerExecutionError
from aigineering.protocol.immutability import deep_thaw
from aigineering.business.claim_check_worker import _candidate
from aigineering.business.claim_review_contracts import (
    BASE_INPUTS,
    expected_draft_checks,
    expected_gate_receipt,
    failure_completion,
    read_inputs,
    validate_draft_checks,
    validate_final_report,
    validate_gate,
)
from aigineering.protocol.actions import action_from_dict, parse_action


class ReviewStageWorker:
    def __init__(self, worker_id, role, delegate=None):
        self.worker_id = worker_id
        self.role = role
        self.delegate = delegate

    def invoke(self, contract, disclosed_assets):
        try:
            failure = failure_completion(self.worker_id, contract)
            if failure is not None:
                return failure
            extra = {
                "assessor": (),
                "reviser": ("draft_checks",),
                "gate": ("draft_checks", "review_report"),
                "semantic": ("draft_checks", "review_report", "structural_receipt"),
            }[self.role]
            inputs, policy = read_inputs(
                contract, disclosed_assets, (*BASE_INPUTS, *extra)
            )
            if policy["actors"][self.role] != self.worker_id:
                raise ValueError("stage actor differs from frozen policy")
            output = {
                "assessor": "draft_checks",
                "reviser": "review_report",
                "gate": "structural_receipt",
                "semantic": "semantic_review",
            }[self.role]
            if tuple(contract.outputs) != (output,):
                raise ValueError("stage output differs from fixed interface")
            if self.role == "assessor":
                return _candidate(
                    self.worker_id,
                    "exec",
                    {
                        "outputs": {
                            output: json.dumps(
                                expected_draft_checks(inputs), sort_keys=True
                            )
                        }
                    },
                )
            if self.role == "gate":
                validate_final_report(inputs, policy)
                return _candidate(
                    self.worker_id,
                    "exec",
                    {
                        "outputs": {
                            output: json.dumps(
                                expected_gate_receipt(inputs), sort_keys=True
                            )
                        }
                    },
                )
            if self.role == "reviser":
                validate_draft_checks(inputs, policy)
                if "review_report" in contract.inputs or any(
                    a.signed_by == self.worker_id and a.name in contract.inputs
                    for a in disclosed_assets
                ):
                    raise ValueError("a revision cannot consume a previous revision")
            else:
                validate_gate(inputs, policy)
            result = self.delegate.invoke(contract, disclosed_assets)
            try:
                action = (
                    action_from_dict(result.parsed_action)
                    if result.parsed_action is not None
                    else parse_action(result.raw_output)
                )
                allowed = action.type in {"exec", "fail"} and (
                    action.type != "exec" or set(action.outputs) == {output}
                )
            except (ValueError, TypeError, KeyError):
                allowed = False
            if not allowed:
                return replace(
                    _candidate(
                        self.worker_id,
                        "fail",
                        {
                            "reason": "bounded review stage cannot publish new work or different outputs"
                        },
                    ),
                    metadata={
                        **deep_thaw(result.metadata),
                        "rejected_model_action": result.raw_output,
                    },
                )
            return result
        except WorkerExecutionError:
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            RecursionError,
            UnicodeError,
        ) as exc:
            return _candidate(
                self.worker_id,
                "fail",
                {"reason": str(exc) or "invalid review stage input"},
            )


def _model(worker_id):
    """Explicit live example profile; a missing key fails before invocation."""
    return LLMWorker(
        worker_id=worker_id,
        model=os.environ.get("AIG_REVIEW_MODEL", "deepseek-flash"),
        base_url=os.environ.get("AIG_REVIEW_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=os.environ["DEEPSEEK_API_KEY"],
        max_retries=0,
        max_output_tokens=8192,
        thinking_mode="disabled",
        timeout=60,
    )


def create_assessor_worker(*, worker_id):
    return ReviewStageWorker(worker_id, "assessor")


def create_revision_worker(*, worker_id):
    return ReviewStageWorker(worker_id, "reviser", _model(worker_id))


def create_review_gate_worker(*, worker_id):
    return ReviewStageWorker(worker_id, "gate")


def create_semantic_worker(*, worker_id):
    return ReviewStageWorker(worker_id, "semantic", _model(worker_id))
