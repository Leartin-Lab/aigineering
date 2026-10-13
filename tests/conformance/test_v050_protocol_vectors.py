"""Consume public v0.5.0 protocol vectors with the Python implementation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import candidate_runtime
import aigineering.protocol.candidate as candidate_protocol

from aigineering.core.acceptance import validate_output_shape
from aigineering.core.causal_allowance import (
    allowance_reservation_id,
    project_contract_allowance_records,
)
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.ids import canonical_json, contract_identity_v3
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import (
    CandidateClaimBinding,
    CandidateEffect,
    CandidateProposal,
    candidate_content_id,
    candidate_proposal_from_dict,
    candidate_proposal_to_dict,
    candidate_signing_bytes,
    create_candidate_proposal,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.asset_graph import DefinitionContentAssertion
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.protocol.types import Asset
from aigineering.protocol.wire import contract_from_dict


VECTOR_PATH = Path("conformance/v0.5.0/protocol-vectors.json")
BOUNDARY_VECTOR_PATH = Path("conformance/v0.5.13/signed-json-boundary-vectors.json")


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


def test_signed_json_boundary_vectors_and_parent_shape() -> None:
    vectors = json.loads(BOUNDARY_VECTOR_PATH.read_text(encoding="utf-8"))
    assert vectors["format"] == "aigineering-signed-json-boundary-v1"
    assert vectors["valid"]["object"]["café"]["nested"][0] == "café"

    effect = CandidateEffect("test.vector", vectors["valid"]["object"])
    assert effect.payload["café"]["nested"][1] == 12
    candidate_vector = vectors["candidate_vector"]
    boundary_candidate = candidate_proposal_from_dict(candidate_vector["candidate"])
    boundary_bytes = candidate_signing_bytes(boundary_candidate)
    signer = Ed25519Signer.from_private_key_hex(
        _vectors()["ed25519_test_key"]["private_key_hex"]
    )
    assert boundary_bytes.decode("utf-8") == candidate_vector["expected_signing_utf8"]
    assert boundary_candidate.id == candidate_vector["expected_id"]
    assert candidate_content_id(boundary_candidate) == candidate_vector["expected_id"]
    assert boundary_candidate.signature == candidate_vector["expected_signature"]
    assert signer.sign(boundary_bytes) == boundary_candidate.signature
    validate_output_shape({"output_shapes": {"numeric": "number"}}, "numeric", "12.5")
    with pytest.raises(ValueError, match="NFC"):
        CandidateProposal(
            id="pending",
            domain_id="genesis:test",
            actor_id="worker:test",
            key_id="key:test",
            signature_kind="ed25519",
            signature="pending",
            effects=(CandidateEffect("test.vector", {}),),
            metadata={"nested": {"café": "value"}},
        )
    for invalid in vectors["invalid_object_keys"]:
        obj = _mapping_from_entries(invalid["entries"])
        with pytest.raises(ValueError, match="NFC|unique after NFC"):
            CandidateEffect("test.vector", obj)
    for invalid in vectors["invalid_signed_values"]:
        if "raw_json" in invalid:
            with pytest.raises(ValueError):
                json.loads(invalid["raw_json"], parse_constant=_reject_json_constant)
        else:
            with pytest.raises(ValueError):
                CandidateEffect("test.vector", {"value": invalid["value"]})

    assertion_fields = {
        "id": "assertion:v1:test",
        "domain_id": "genesis:test",
        "definition_id": "definition:v1:test",
        "content_id": "content:v1:test",
        "relation_type": "supports",
        "actor_id": "worker:test",
        "key_id": "key:test",
        "signature_kind": "ed25519",
        "signature": "test",
    }
    with pytest.raises(ValueError, match="NFC"):
        DefinitionContentAssertion(
            **assertion_fields, evidence={"nested": {"café": "value"}}
        )

    base = _vectors()["contract_candidate"]["value"]
    duplicate_parents = candidate_proposal_from_dict(
        {**base, "causal_parents": ["record:one", "record:one"]}
    )
    assert duplicate_parents.causal_parents == ("record:one", "record:one")
    for invalid in vectors["invalid_parents"]:
        if "raw_json" in invalid:
            with pytest.raises(ValueError):
                json.loads(invalid["raw_json"], parse_constant=_reject_json_constant)
            continue
        candidate_value = {**base, "causal_parents": invalid["value"]}
        with pytest.raises(ValueError, match="causal_parents"):
            candidate_proposal_from_dict(candidate_value)

    container_values = {
        "scalar-string": "record:alpha",
        "bytes": b"record:alpha",
        "mapping": {"parent": "record:alpha"},
        "integer": 1,
    }
    for name in vectors["invalid_parent_containers"]:
        bad = container_values[name]
        with pytest.raises(ValueError, match="causal_parents"):
            candidate_proposal_from_dict({**base, "causal_parents": bad})
        with pytest.raises(ValueError, match="causal_parents"):
            CandidateProposal(
                id="pending",
                domain_id="genesis:test",
                actor_id="worker:test",
                key_id="key:test",
                signature_kind="ed25519",
                signature="pending",
                effects=(CandidateEffect("test.vector", {}),),
                causal_parents=bad,
            )

    with pytest.raises(ValueError, match="causal_parents"):
        CandidateProposal(
            id="pending",
            domain_id="genesis:test",
            actor_id="worker:test",
            key_id="key:test",
            signature_kind="ed25519",
            signature="pending",
            effects=(CandidateEffect("test.vector", {}),),
            causal_parents=("",),
        )


def test_legacy_signed_candidate_parents_remain_readable_and_replay_is_rejected(
    tmp_path, monkeypatch
):
    database = tmp_path / "legacy-causal-parent.db"
    store = SQLiteStore(database)
    setup = candidate_runtime(store)
    with monkeypatch.context() as old_protocol:
        old_protocol.setattr(
            candidate_protocol,
            "_validate_causal_parents",
            lambda value, *, path: tuple(value),
        )
        legacy_candidate = create_candidate_proposal(
            domain_id=setup.genesis.id,
            actor_id=setup.actor_key.actor_id,
            key_id=setup.actor_key.key_id,
            effects=(
                asset_proposal_effect(
                    Asset(id="", name="legacy-signed-parent", content="value")
                ),
            ),
            signer=setup.signer,
            causal_parents=(1 << 60,),
            idempotency_key="legacy-signed-parent",
        )
        legacy_wire = candidate_proposal_to_dict(legacy_candidate)
        decision = CandidateCommitter(store, store).commit(
            legacy_candidate, setup.genesis
        )
        assert decision.accepted
        assert decision.assets
        history = tuple(record for _, record in store.scan_runtime_records())
        receipt = next(
            row for row in history if row.record_type == "candidate.received"
        )
        committed_asset = next(
            row for row in history if row.record_type == "asset.committed"
        )
        assert receipt.payload["candidate_id"] == legacy_candidate.id
        assert receipt.causal_parents == (1 << 60,)
        assert receipt.id in committed_asset.causal_parents
    assert store.get_all_assets()
    before_records = tuple(record.id for _, record in store.scan_runtime_records())
    before = store.runtime_materialization_digest()
    revision = store.get_runtime_revision()
    store.close()

    reopened = SQLiteStore(database)
    try:
        with pytest.raises(ValueError, match="causal_parents"):
            candidate_proposal_from_dict(legacy_wire)
        assert reopened.get_runtime_revision() == revision
        stored_records = tuple(
            record.id for _, record in reopened.scan_runtime_records()
        )
        assert stored_records == before_records
        assert reopened.get_all_assets()
        assert reopened.rebuild_runtime_materializations(strict=True) == before
        assert reopened.runtime_materialization_digest() == before
    finally:
        reopened.close()


def test_valid_string_parent_candidate_commits_and_replays(temp_sqlite_store):
    setup = candidate_runtime(temp_sqlite_store)
    candidate = create_candidate_proposal(
        domain_id=setup.genesis.id,
        actor_id=setup.actor_key.actor_id,
        key_id=setup.actor_key.key_id,
        effects=(
            asset_proposal_effect(
                Asset(id="", name="signed-parent-control", content="value")
            ),
        ),
        signer=setup.signer,
        causal_parents=("record:unresolved", "record:unresolved"),
        idempotency_key="signed-parent-control",
    )
    committer = CandidateCommitter(temp_sqlite_store, temp_sqlite_store)

    first = committer.commit(candidate, setup.genesis)
    first_record_ids = tuple(
        record.id for _, record in temp_sqlite_store.scan_runtime_records()
    )
    replay = committer.commit(candidate, setup.genesis)

    assert first.accepted
    assert replay.accepted
    assert replay.candidate_id == candidate.id
    assert (
        tuple(record.id for _, record in temp_sqlite_store.scan_runtime_records())
        == first_record_ids
    )


def _mapping_from_entries(entries):
    result = {}
    for key, value in entries:
        if isinstance(value, dict) and "entries" in value:
            value = _mapping_from_entries(value["entries"])
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(value)
