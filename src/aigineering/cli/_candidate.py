"""Shared local actor assembly for CLI Candidate publication."""

from __future__ import annotations

from aigineering.cli.domain import load_actor_signer
from aigineering.core.commitment import CandidateCommitter, CommitmentDecision
from aigineering.core.domain import load_genesis
from aigineering.protocol.candidate import CandidateEffect, create_candidate_proposal
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import contract_to_dict


def contract_declaration_effect(contract: Contract) -> CandidateEffect:
    return CandidateEffect("contract.declare", {"contract": contract_to_dict(contract)})


def asset_proposal_effect(asset: Asset) -> CandidateEffect:
    return CandidateEffect(
        "asset.propose",
        {
            "asset": {
                "content": asset.content,
                "content_type": asset.content_type,
                "disclosure_view": asset.disclosure_view,
                "name": asset.name,
                "origin": asset.origin,
                "promptable": asset.promptable,
                "source_uri": asset.source_uri,
                "trust_tier": asset.trust_tier,
            }
        },
    )


def commit_local_effect(
    store,
    effect: CandidateEffect,
    *,
    idempotency_key: str,
) -> CommitmentDecision:
    genesis = load_genesis(store)
    signer = load_actor_signer()
    try:
        actor_key = next(
            key
            for key in genesis.root_keys
            if key.public_key == signer.signer_id and not key.revoked
        )
    except StopIteration as exc:
        raise ValueError("local actor key is not authorized by domain Genesis") from exc
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
        effects=[effect],
        signer=signer,
        idempotency_key=idempotency_key,
    )
    return CandidateCommitter(store, store).commit(candidate)


def require_accepted(decision: CommitmentDecision) -> CommitmentDecision:
    if decision.accepted:
        return decision
    rejection = next(
        record
        for record in decision.runtime_records
        if record.record_type.endswith("rejected")
    )
    raise ValueError(str(rejection.payload["reason"]))
