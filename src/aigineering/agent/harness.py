"""Signed Candidate adapter for existing agent harnesses."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aigineering.agent.worker import compile_worker_envelope
from aigineering.core.signing import Ed25519Signer, Signer
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateProposal,
    candidate_proposal_to_dict,
    create_candidate_proposal,
)
from aigineering.protocol.effect_builders import (
    worker_claim_effect,
    worker_claim_renewal_effect,
)
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.wire import asset_from_dict, contract_from_dict


@dataclass(frozen=True)
class HarnessCandidateAdapter:
    """Turn one harness action into authenticated Aigineering Candidates."""

    domain_id: str
    actor_key: ActorKey
    signer: Signer

    def __post_init__(self) -> None:
        if not self.domain_id:
            raise ValueError("domain_id must not be empty")
        if (
            self.actor_key.kind != self.signer.kind
            or self.actor_key.public_key != self.signer.signer_id
        ):
            raise ValueError("harness signer does not match its actor key")
        if "worker.submit" not in self.actor_key.capabilities:
            raise ValueError("harness actor key requires 'worker.submit'")

    @classmethod
    def from_private_key_hex(
        cls,
        *,
        domain_id: str,
        actor_id: str,
        key_id: str,
        private_key_hex: str,
        capabilities: tuple[str, ...] = ("worker.submit",),
    ) -> HarnessCandidateAdapter:
        """Construct an adapter while keeping private material harness-local."""
        signer = Ed25519Signer.from_private_key_hex(private_key_hex)
        actor_key = ActorKey(
            actor_id=actor_id,
            key_id=key_id,
            kind=signer.kind,
            public_key=signer.signer_id,
            capabilities=capabilities,
        )
        return cls(domain_id, actor_key, signer)

    @property
    def worker_id(self) -> str:
        return self.actor_key.actor_id

    def claim_candidate(
        self,
        *,
        request_id: str,
        contract_id: str | None = None,
        lease_seconds: int = 60,
    ) -> CandidateProposal:
        """Sign a single-use claim command for local or HTTP transport."""
        return self._command_candidate(
            worker_claim_effect(
                self.worker_id,
                contract_id=contract_id,
                lease_seconds=lease_seconds,
            ),
            request_id=request_id,
        )

    def renewal_candidate(
        self,
        package: WorkerPackage | str | Mapping[str, Any],
        *,
        request_id: str,
        lease_seconds: int = 60,
    ) -> CandidateProposal:
        """Sign one fenced renewal command for a claimed package."""
        package = _worker_package(package)
        if not package.claim_id or package.claim_epoch < 1:
            raise ValueError("renewal requires a claimed WorkerPackage")
        return self._command_candidate(
            worker_claim_renewal_effect(
                self.worker_id,
                package.claim_id,
                package.claim_epoch,
                lease_seconds=lease_seconds,
            ),
            request_id=request_id,
        )

    def result_candidate(
        self,
        package: WorkerPackage | str | Mapping[str, Any],
        raw_action: str,
        *,
        usage_metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> CandidateProposal:
        """Compile and sign one harness result against its exact package."""
        package = _worker_package(package)
        if not package.claim_id or package.claim_epoch < 1:
            raise ValueError("result requires a claimed WorkerPackage")
        contract = contract_from_dict(package.contract)
        assets = tuple(
            asset_from_dict(item)
            for item in (*package.disclosed_assets, *package.method_context_assets)
        )
        envelope = CandidateEnvelope(
            contract_id=package.contract_id,
            worker_id=self.worker_id,
            raw_output=raw_action,
            package_id=package.package_id,
            claim_id=package.claim_id,
            claim_epoch=package.claim_epoch,
            idempotency_key=(
                idempotency_key
                or f"result:{package.package_id}:{package.claim_id}:{package.claim_epoch}"
            ),
            usage_metadata=usage_metadata,
        )
        return compile_worker_envelope(
            envelope,
            domain_id=self.domain_id,
            actor_key=self.actor_key,
            signer=self.signer,
            contract=contract,
            disclosed_assets=assets,
            allowance=package.budget_remaining,
        )

    def _command_candidate(self, effect, *, request_id: str) -> CandidateProposal:
        if not request_id.strip():
            raise ValueError("request_id must not be empty")
        return create_candidate_proposal(
            domain_id=self.domain_id,
            actor_id=self.actor_key.actor_id,
            key_id=self.actor_key.key_id,
            effects=(effect,),
            signer=self.signer,
            idempotency_key=request_id,
        )


def candidate_dict(candidate: CandidateProposal) -> dict[str, Any]:
    """Serialize a signed Candidate for an HTTP JSON body."""
    return candidate_proposal_to_dict(candidate)


def candidate_json(candidate: CandidateProposal) -> str:
    """Serialize a signed Candidate for CLI or raw JSON transport."""
    return json.dumps(
        candidate_dict(candidate),
        sort_keys=True,
        ensure_ascii=False,
    )


def _worker_package(
    package: WorkerPackage | str | Mapping[str, Any],
) -> WorkerPackage:
    if isinstance(package, WorkerPackage):
        return package
    if isinstance(package, str):
        return WorkerPackage.from_json(package)
    if isinstance(package, Mapping):
        return WorkerPackage.from_json(
            json.dumps(dict(package), sort_keys=True, ensure_ascii=False)
        )
    raise TypeError("package must be a WorkerPackage, JSON string, or mapping")
