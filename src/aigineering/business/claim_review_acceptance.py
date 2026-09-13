"""Final acceptance adapter for the bounded claim-review workflow."""

from __future__ import annotations

import json
import re

from aigineering.business.claim_check_worker import _candidate, _read_json
from aigineering.business.claim_review_contracts import (
    failure_completion,
    read_inputs,
    require_actor,
    validate_gate,
)
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.types import Candidate, Contract


class ClaimReviewAcceptanceWorker:
    def __init__(self, worker_id: str):
        self.worker_id = worker_id

    def registration(self):
        return WorkerRegistration(
            self.worker_id,
            capabilities=("claim.review.accept",),
            pools=("verification",),
            profile_id="claim-review-acceptance-v1",
        )

    def invoke(self, contract: Contract, disclosed_assets) -> Candidate:
        completion = failure_completion(self.worker_id, contract)
        if completion is not None:
            return completion
        try:
            if (
                tuple(contract.outputs) != ("acceptance_receipt",)
                or not contract.parent_id
            ):
                raise ValueError("acceptance Contract binding is invalid")
            inputs, policy = read_inputs(
                contract,
                disclosed_assets,
                (
                    "research_result",
                    "draft_report",
                    "review_policy",
                    "draft_checks",
                    "review_report",
                    "structural_receipt",
                    "semantic_review",
                ),
            )
            # The signed task intent must identify one exact target. Native
            # planning inserts intermediate parents, so parent_id is not a
            # substitute. Commitment independently validates target authority.
            targets = set(
                re.findall(
                    r"task:v[1-9][0-9]*:[0-9a-f]{64}(?![0-9a-f])", contract.description
                )
            )
            if len(targets) != 1:
                raise ValueError("expected one exact attestation target Contract ID")
            target_id = targets.pop()
            actors = policy["actors"]
            if actors["acceptor"] != self.worker_id:
                raise ValueError("acceptance actor does not match policy")
            validate_gate(inputs, policy)
            require_actor(inputs["semantic_review"], actors["semantic"])
            semantic = _read_json(inputs["semantic_review"].content)
            expected = {
                "schema",
                "source_asset_id",
                "report_asset_id",
                "policy_asset_id",
                "gate_receipt_asset_id",
                "verdict",
                "reason",
            }
            if not isinstance(semantic, dict) or set(semantic) != expected:
                raise ValueError("semantic review fields are not exact")
            if (
                semantic["schema"] != "claim-semantic-review-v1"
                or semantic["source_asset_id"] != inputs["research_result"].id
                or semantic["report_asset_id"] != inputs["review_report"].id
                or semantic["policy_asset_id"] != inputs["review_policy"].id
                or semantic["gate_receipt_asset_id"] != inputs["structural_receipt"].id
                or semantic["verdict"] != "accepted"
                or not isinstance(semantic["reason"], str)
                or not semantic["reason"]
            ):
                raise ValueError("semantic review was not accepted")
            receipt = {
                "source_asset_id": inputs["research_result"].id,
                "draft_asset_id": inputs["draft_report"].id,
                "report_asset_id": inputs["review_report"].id,
                "policy_asset_id": inputs["review_policy"].id,
                "structural_receipt_asset_id": inputs["structural_receipt"].id,
                "semantic_review_asset_id": inputs["semantic_review"].id,
                "revision_depth": 1,
            }
            return Candidate(
                worker_id=self.worker_id,
                raw_output="/attest "
                + json.dumps(
                    {
                        "contract_id": target_id,
                        "output_name": "review_report",
                        "asset_id": inputs["review_report"].id,
                        "verdict": "accepted",
                        "policy_version": "claim-review-closure-v1",
                        "evidence_asset_ids": sorted(
                            [
                                inputs["research_result"].id,
                                inputs["draft_report"].id,
                                inputs["review_policy"].id,
                            ]
                        ),
                        "outputs": {
                            "acceptance_receipt": json.dumps(receipt, sort_keys=True)
                        },
                    },
                    sort_keys=True,
                ),
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
                {"reason": "claim review acceptance closure failed"},
            )


def create_acceptance_worker(*, worker_id: str):
    return ClaimReviewAcceptanceWorker(worker_id)
