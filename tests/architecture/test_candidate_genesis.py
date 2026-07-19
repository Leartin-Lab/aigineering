"""Architecture gates for Change 001's authentication boundary."""

from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from aigineering.core.signing import DeterministicSigner, Signer, Verifier
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    candidate_received_record,
    candidate_proposal_from_dict,
    candidate_proposal_to_dict,
    create_candidate_proposal,
    create_genesis_manifest,
    genesis_manifest_from_dict,
    genesis_manifest_to_dict,
    validate_genesis_manifest,
    verify_candidate_proposal,
)


class _TestSigner(Signer):
    """Test-only signature primitive for dependency-free protocol tests."""

    kind = "test-signature"

    @property
    def signer_id(self) -> str:
        return "test-public-key"

    def sign(self, data: bytes) -> str:
        return hashlib.sha256(self.signer_id.encode() + data).hexdigest()


class _TestVerifier(Verifier):
    def verify(self, data: bytes, signature: str, signer_id: str) -> bool:
        expected = hashlib.sha256(signer_id.encode() + data).hexdigest()
        return signature == expected


def _verifier_factory(kind: str) -> Verifier:
    assert kind == _TestSigner.kind
    return _TestVerifier()


def _genesis_and_candidate():
    signer = _TestSigner()
    genesis = create_genesis_manifest(
        "test-domain",
        [
            ActorKey(
                actor_id="human:owner",
                key_id="root-1",
                kind=signer.kind,
                public_key=signer.signer_id,
                capabilities=("contract.publish",),
            )
        ],
        "policy:sha256:test",
    )
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id="human:owner",
        key_id="root-1",
        effects=[
            CandidateEffect(
                "contract.declare",
                {"contract": {"name": "root", "outputs": ["report"]}},
            )
        ],
        signer=signer,
        idempotency_key="publish-root",
    )
    return genesis, candidate


def test_genesis_and_candidate_ids_are_deterministic():
    first_genesis, first_candidate = _genesis_and_candidate()
    second_genesis, second_candidate = _genesis_and_candidate()

    assert first_genesis.id == second_genesis.id
    assert first_candidate.id == second_candidate.id
    validate_genesis_manifest(first_genesis)
    verify_candidate_proposal(
        first_candidate, first_genesis, verifier_factory=_verifier_factory
    )


def test_effect_payload_is_deeply_immutable():
    effect = CandidateEffect("asset.propose", {"items": [{"value": 1}]})

    with pytest.raises(TypeError):
        effect.payload["items"][0]["value"] = 2


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda candidate: replace(candidate, domain_id="genesis:other"), "domain"),
        (lambda candidate: replace(candidate, actor_id="worker:other"), "actor/key"),
        (lambda candidate: replace(candidate, key_id="other"), "actor/key"),
        (lambda candidate: replace(candidate, signature="asig_bad"), "signature"),
        (
            lambda candidate: replace(
                candidate,
                effects=(CandidateEffect("contract.declare", {"changed": True}),),
            ),
            "content id",
        ),
    ],
)
def test_candidate_verification_fails_closed(mutation, message):
    genesis, candidate = _genesis_and_candidate()

    with pytest.raises(ValueError, match=message):
        verify_candidate_proposal(
            mutation(candidate), genesis, verifier_factory=_verifier_factory
        )


def test_receipt_is_not_effect_acceptance():
    genesis, candidate = _genesis_and_candidate()

    record = candidate_received_record(
        candidate, genesis, verifier_factory=_verifier_factory
    )

    assert record.record_type == "candidate.received"
    assert record.payload["candidate_id"] == candidate.id
    assert "contract" not in record.payload


def test_candidate_metadata_is_signed_and_receipted():
    genesis, candidate = _genesis_and_candidate()
    signer = _TestSigner()
    signed = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=candidate.actor_id,
        key_id=candidate.key_id,
        effects=candidate.effects,
        signer=signer,
        idempotency_key=candidate.idempotency_key,
        metadata={"model": "test-model", "total_tokens": 17},
    )

    receipt = candidate_received_record(
        signed, genesis, verifier_factory=_verifier_factory
    )
    assert dict(receipt.payload["metadata"]) == {
        "model": "test-model",
        "total_tokens": 17,
    }
    with pytest.raises(ValueError, match="content id"):
        verify_candidate_proposal(
            replace(signed, metadata={"model": "tampered"}),
            genesis,
            verifier_factory=_verifier_factory,
        )


def test_revoked_genesis_key_fails_closed():
    genesis, candidate = _genesis_and_candidate()
    revoked = replace(genesis.root_keys[0], revoked=True)
    revoked_genesis = create_genesis_manifest(
        genesis.domain, [revoked], genesis.policy_hash
    )
    rebound = replace(candidate, domain_id=revoked_genesis.id)

    with pytest.raises(ValueError, match="revoked"):
        verify_candidate_proposal(
            rebound, revoked_genesis, verifier_factory=_verifier_factory
        )


def test_deterministic_seal_is_not_actor_authentication():
    signer = DeterministicSigner("not-a-private-key")
    genesis = create_genesis_manifest(
        "insecure-domain",
        [ActorKey("actor", "key", signer.kind, signer.signer_id)],
        "policy:test",
    )
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id="actor",
        key_id="key",
        effects=[CandidateEffect("asset.propose", {"name": "result"})],
        signer=signer,
    )

    with pytest.raises(ValueError, match="cannot authenticate"):
        verify_candidate_proposal(candidate, genesis)


def test_wire_round_trip_preserves_authenticated_bytes():
    genesis, candidate = _genesis_and_candidate()

    decoded_genesis = genesis_manifest_from_dict(genesis_manifest_to_dict(genesis))
    decoded_candidate = candidate_proposal_from_dict(
        candidate_proposal_to_dict(candidate)
    )

    assert decoded_genesis == genesis
    assert decoded_candidate == candidate
    verify_candidate_proposal(
        decoded_candidate,
        decoded_genesis,
        verifier_factory=_verifier_factory,
    )
