"""Consume public v0.5.2 Asset graph vectors with protocol-only code."""

from __future__ import annotations

import json
from pathlib import Path

from aigineering.core.signing import Ed25519Signer
from aigineering.protocol.asset_graph import (
    assertion_signing_bytes,
    content_object_from_dict,
    definition_content_assertion_from_dict,
    definition_content_assertion_id,
    definition_signing_bytes,
    signed_definition_from_dict,
    signed_definition_id,
    validate_content_object,
    verify_definition_content_assertion,
    verify_signed_definition,
)
from aigineering.protocol.candidate import ActorKey

VECTOR_PATH = Path("conformance/v0.5.2/asset-graph-vectors.json")


def test_v052_language_neutral_asset_graph_vectors() -> None:
    vectors = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
    key_data = vectors["ed25519_test_key"]
    signer = Ed25519Signer.from_private_key_hex(key_data["private_key_hex"])
    assert signer.signer_id == key_data["public_key_hex"]
    key = ActorKey(
        actor_id="worker:vector",
        key_id="vector-key",
        kind="ed25519",
        public_key=key_data["public_key_hex"],
    )

    content = content_object_from_dict(vectors["content"]["value"])
    validate_content_object(content)

    definition = signed_definition_from_dict(vectors["definition"]["value"])
    assert (
        definition_signing_bytes(definition).decode()
        == vectors["definition"]["expected_signing_utf8"]
    )
    assert signed_definition_id(definition) == definition.id
    verify_signed_definition(definition, key)

    assertion = definition_content_assertion_from_dict(vectors["assertion"]["value"])
    assert (
        assertion_signing_bytes(assertion).decode()
        == vectors["assertion"]["expected_signing_utf8"]
    )
    assert definition_content_assertion_id(assertion) == assertion.id
    verify_definition_content_assertion(assertion, key)
