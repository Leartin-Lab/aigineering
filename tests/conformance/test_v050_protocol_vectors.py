"""Consume public v0.5.0 protocol vectors with the Python implementation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aigineering.core.causal_allowance import (
    allowance_reservation_id,
    project_contract_allowance_records,
)
from aigineering.core.ids import canonical_json, contract_identity_v3
from aigineering.core.signing import Ed25519Signer
from aigineering.protocol.candidate import (
    CandidateClaimBinding,
    CandidateEffect,
    CandidateProposal,
    candidate_content_id,
    candidate_proposal_from_dict,
    candidate_signing_bytes,
    create_candidate_proposal,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.wire import contract_from_dict


VECTOR_PATH = Path("conformance/v0.5.0/protocol-vectors.json")


def _vectors() -> dict:
    return json.loads(VECTOR_PATH.read_text(encoding="utf-8"))


def test_canonical_json_and_ed25519_key_vector() -> None:
    vectors = _vectors()
    assert (
        canonical_json(vectors["canonical_json"]["input"])
        == vectors["canonical_json"]["expected"]
    )
    signer = Ed25519Signer.from_private_key_hex(
        vectors["ed25519_test_key"]["private_key_hex"]
    )
    assert signer.signer_id == vectors["ed25519_test_key"]["public_key_hex"]


def test_genesis_contract_candidate_and_attestation_vectors() -> None:
    vectors = _vectors()
    genesis = genesis_manifest_from_dict(vectors["genesis"])
    contract = contract_from_dict(vectors["contract"]["value"])
    assert contract.id == vectors["contract"]["expected_id"]
    assert contract_identity_v3(contract) == contract.id

    signer = Ed25519Signer.from_private_key_hex(
        vectors["ed25519_test_key"]["private_key_hex"]
    )
    for name in ("contract_candidate", "attestation_candidate"):
        item = vectors[name]
        candidate = candidate_proposal_from_dict(item["value"])
        assert (
            candidate_signing_bytes(candidate).decode() == item["expected_signing_utf8"]
        )
        assert signer.sign(candidate_signing_bytes(candidate)) == candidate.signature
        verify_candidate_proposal(candidate, genesis)


def test_allowance_identity_and_root_grant_vector() -> None:
    vectors = _vectors()
    item = vectors["allowance"]
    assert (
        allowance_reservation_id(
            item["source_contract_id"], item["child_contract_id"], item["purpose"]
        )
        == item["expected_reservation_id"]
    )

    contract = contract_from_dict(vectors["contract"]["value"])
    candidate = candidate_proposal_from_dict(vectors["contract_candidate"]["value"])
    grant = project_contract_allowance_records(
        (contract,), (), (), causal_parent=candidate.id
    )[0]
    expected = item["root_grant"]
    assert grant.id == expected["record_id"]
    assert grant.record_type == expected["record_type"]
    assert dict(grant.payload) == expected["payload"]
    assert list(grant.causal_parents) == expected["causal_parents"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"value": 1.5}, "floating-point"),
        ({1: "value"}, "keys must be strings"),
        ({"value": 1 << 53}, "interoperable JSON range"),
        ({"value": {"unordered"}}, "unsupported signed JSON value"),
    ],
)
def test_signed_candidate_json_rejects_non_interoperable_values(
    payload, message
) -> None:
    with pytest.raises(ValueError, match=message):
        CandidateEffect("test.vector", payload)


def test_candidate_typed_integers_are_interoperable_and_versioned() -> None:
    for invalid_epoch in (1 << 53, True, "1"):
        with pytest.raises(ValueError, match="interoperable JSON integer"):
            CandidateClaimBinding("task:one", "claim:one", invalid_epoch, "pkg:one")

    effect = CandidateEffect("test.vector", {"value": 1})
    with pytest.raises(ValueError, match="unsupported Candidate protocol_version"):
        CandidateProposal(
            id="pending",
            domain_id="genesis:one",
            actor_id="worker:one",
            key_id="key:one",
            signature_kind="ed25519",
            signature="pending",
            effects=(effect,),
            protocol_version=2,
        )

    vectors = _vectors()
    wire = dict(vectors["contract_candidate"]["value"])
    wire["protocol_version"] = "1"
    with pytest.raises(ValueError, match="unsupported Candidate protocol_version"):
        candidate_proposal_from_dict(wire)


def test_candidate_content_id_uses_exact_signing_bytes() -> None:
    candidate = candidate_proposal_from_dict(_vectors()["contract_candidate"]["value"])
    assert candidate_content_id(candidate) == candidate.id


def test_candidate_identity_and_signature_normalize_equivalent_unicode() -> None:
    vectors = _vectors()
    signer = Ed25519Signer.from_private_key_hex(
        vectors["ed25519_test_key"]["private_key_hex"]
    )
    effect = CandidateEffect("test.vector", {"value": "same"})
    common = {
        "domain_id": vectors["genesis"]["id"],
        "actor_id": "worker:vector",
        "key_id": "vector-key",
        "effects": (effect,),
        "signer": signer,
        "idempotency_key": "unicode-vector",
    }
    composed = create_candidate_proposal(**common, metadata={"name": "café"})
    decomposed = create_candidate_proposal(**common, metadata={"name": "café"})
    assert composed.id == decomposed.id
    assert composed.signature == decomposed.signature
    assert candidate_signing_bytes(composed) == candidate_signing_bytes(decomposed)
