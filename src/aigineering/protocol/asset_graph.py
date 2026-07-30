"""Signed definition and content graph protocol values."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from aigineering.core.ids import canonical_json, compute_content_hash
from aigineering.core.signing import Signer, Verifier, create_verifier
from aigineering.protocol.candidate import ActorKey, _validate_signed_json
from aigineering.protocol.immutability import deep_freeze, deep_thaw

ASSET_GRAPH_SCHEMA_VERSION = 1


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return unicodedata.normalize("NFC", canonical_json(value)).encode("utf-8")


@dataclass(frozen=True)
class ContentObject:
    """Content-addressed normalized text, independent of definition or signer."""

    id: str
    content: str
    schema_version: int = ASSET_GRAPH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ASSET_GRAPH_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported ContentObject schema_version {self.schema_version}"
            )


def content_object_id(content: str) -> str:
    return f"content:v1:{compute_content_hash(content)}"


def create_content_object(content: str) -> ContentObject:
    return ContentObject(
        id=content_object_id(content),
        content=unicodedata.normalize("NFC", content),
    )


def validate_content_object(value: ContentObject) -> None:
    expected = content_object_id(value.content)
    if value.id != expected:
        raise ValueError(
            f"ContentObject id mismatch: supplied {value.id!r}, expected {expected!r}"
        )


@dataclass(frozen=True)
class SignedAssetDefinition:
    """One actor-signed semantic definition of an Asset."""

    id: str
    domain_id: str
    name: str
    description: str
    content_type: str
    source_kind: str
    source_uri: str
    actor_id: str
    key_id: str
    signature_kind: str
    signature: str
    schema_version: int = ASSET_GRAPH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "domain_id",
            "name",
            "content_type",
            "source_kind",
            "actor_id",
            "key_id",
            "signature_kind",
        ):
            if not getattr(self, field_name):
                raise ValueError(
                    f"SignedAssetDefinition.{field_name} must not be empty"
                )
        if self.schema_version != ASSET_GRAPH_SCHEMA_VERSION:
            raise ValueError(
                "unsupported SignedAssetDefinition schema_version "
                f"{self.schema_version}"
            )


def definition_effective_payload(
    definition: SignedAssetDefinition,
) -> dict[str, Any]:
    return {
        "actor_id": definition.actor_id,
        "content_type": definition.content_type,
        "description": definition.description,
        "domain_id": definition.domain_id,
        "key_id": definition.key_id,
        "name": definition.name,
        "schema_version": definition.schema_version,
        "signature_kind": definition.signature_kind,
        "source_kind": definition.source_kind,
        "source_uri": definition.source_uri,
    }


def definition_signing_bytes(definition: SignedAssetDefinition) -> bytes:
    return _canonical_bytes(definition_effective_payload(definition))


def signed_definition_id(definition: SignedAssetDefinition) -> str:
    identified = {
        **definition_effective_payload(definition),
        "signature": definition.signature,
    }
    return "definition:v1:" + hashlib.sha256(_canonical_bytes(identified)).hexdigest()


def create_signed_definition(
    *,
    domain_id: str,
    name: str,
    description: str,
    content_type: str,
    source_kind: str,
    source_uri: str,
    actor_id: str,
    key_id: str,
    signer: Signer,
) -> SignedAssetDefinition:
    provisional = SignedAssetDefinition(
        id="pending",
        domain_id=domain_id,
        name=name,
        description=description,
        content_type=content_type,
        source_kind=source_kind,
        source_uri=source_uri,
        actor_id=actor_id,
        key_id=key_id,
        signature_kind=signer.kind,
        signature="pending",
    )
    signed = replace(
        provisional, signature=signer.sign(definition_signing_bytes(provisional))
    )
    return replace(signed, id=signed_definition_id(signed))


def verify_signed_definition(
    definition: SignedAssetDefinition,
    actor_key: ActorKey,
    *,
    verifier_factory: type[Verifier] | Any = create_verifier,
) -> None:
    if (definition.actor_id, definition.key_id) != (
        actor_key.actor_id,
        actor_key.key_id,
    ):
        raise ValueError("definition actor/key does not match authorized key")
    if actor_key.revoked:
        raise ValueError("definition actor key is revoked")
    if definition.signature_kind != actor_key.kind:
        raise ValueError("definition signature kind does not match actor key")
    if definition.signature_kind in {"deterministic", "asig_"}:
        raise ValueError("definition requires an authenticating signature")
    if definition.id != signed_definition_id(definition):
        raise ValueError("definition id does not match signed payload")
    verifier = verifier_factory(definition.signature_kind)
    if not verifier.verify(
        definition_signing_bytes(definition),
        definition.signature,
        actor_key.public_key,
    ):
        raise ValueError("definition signature verification failed")


@dataclass(frozen=True)
class DefinitionContentAssertion:
    """One signed edge between a definition and a content object."""

    id: str
    domain_id: str
    definition_id: str
    content_id: str
    relation_type: str
    actor_id: str
    key_id: str
    signature_kind: str
    signature: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = ASSET_GRAPH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "domain_id",
            "definition_id",
            "content_id",
            "relation_type",
            "actor_id",
            "key_id",
            "signature_kind",
        ):
            if not getattr(self, field_name):
                raise ValueError(
                    f"DefinitionContentAssertion.{field_name} must not be empty"
                )
        if not self.definition_id.startswith("definition:v1:"):
            raise ValueError("assertion definition_id must use definition:v1")
        if not self.content_id.startswith("content:v1:"):
            raise ValueError("assertion content_id must use content:v1")
        if self.schema_version != ASSET_GRAPH_SCHEMA_VERSION:
            raise ValueError(
                "unsupported DefinitionContentAssertion schema_version "
                f"{self.schema_version}"
            )
        _validate_signed_json(self.evidence, path="DefinitionContentAssertion.evidence")
        object.__setattr__(self, "evidence", deep_freeze(dict(self.evidence)))


def assertion_effective_payload(
    assertion: DefinitionContentAssertion,
) -> dict[str, Any]:
    return {
        "actor_id": assertion.actor_id,
        "content_id": assertion.content_id,
        "definition_id": assertion.definition_id,
        "domain_id": assertion.domain_id,
        "evidence": deep_thaw(assertion.evidence),
        "key_id": assertion.key_id,
        "relation_type": assertion.relation_type,
        "schema_version": assertion.schema_version,
        "signature_kind": assertion.signature_kind,
    }


def assertion_signing_bytes(assertion: DefinitionContentAssertion) -> bytes:
    return _canonical_bytes(assertion_effective_payload(assertion))


def definition_content_assertion_id(assertion: DefinitionContentAssertion) -> str:
    identified = {
        **assertion_effective_payload(assertion),
        "signature": assertion.signature,
    }
    return "assertion:v1:" + hashlib.sha256(_canonical_bytes(identified)).hexdigest()


def create_definition_content_assertion(
    *,
    domain_id: str,
    definition_id: str,
    content_id: str,
    relation_type: str,
    actor_id: str,
    key_id: str,
    signer: Signer,
    evidence: Mapping[str, Any] | None = None,
) -> DefinitionContentAssertion:
    provisional = DefinitionContentAssertion(
        id="pending",
        domain_id=domain_id,
        definition_id=definition_id,
        content_id=content_id,
        relation_type=relation_type,
        actor_id=actor_id,
        key_id=key_id,
        signature_kind=signer.kind,
        signature="pending",
        evidence=evidence or {},
    )
    signed = replace(
        provisional, signature=signer.sign(assertion_signing_bytes(provisional))
    )
    return replace(signed, id=definition_content_assertion_id(signed))


def verify_definition_content_assertion(
    assertion: DefinitionContentAssertion,
    actor_key: ActorKey,
    *,
    verifier_factory: type[Verifier] | Any = create_verifier,
) -> None:
    if (assertion.actor_id, assertion.key_id) != (
        actor_key.actor_id,
        actor_key.key_id,
    ):
        raise ValueError("assertion actor/key does not match authorized key")
    if actor_key.revoked:
        raise ValueError("assertion actor key is revoked")
    if assertion.signature_kind != actor_key.kind:
        raise ValueError("assertion signature kind does not match actor key")
    if assertion.signature_kind in {"deterministic", "asig_"}:
        raise ValueError("assertion requires an authenticating signature")
    if assertion.id != definition_content_assertion_id(assertion):
        raise ValueError("assertion id does not match signed payload")
    verifier = verifier_factory(assertion.signature_kind)
    if not verifier.verify(
        assertion_signing_bytes(assertion),
        assertion.signature,
        actor_key.public_key,
    ):
        raise ValueError("assertion signature verification failed")


def content_object_to_dict(value: ContentObject) -> dict[str, Any]:
    return {
        "content": value.content,
        "id": value.id,
        "schema_version": value.schema_version,
    }


def content_object_from_dict(value: Mapping[str, Any]) -> ContentObject:
    result = ContentObject(
        id=str(value.get("id", "")),
        content=str(value.get("content", "")),
        schema_version=value.get("schema_version", ASSET_GRAPH_SCHEMA_VERSION),
    )
    validate_content_object(result)
    return result


def signed_definition_to_dict(value: SignedAssetDefinition) -> dict[str, Any]:
    return {
        "id": value.id,
        "signature": value.signature,
        **definition_effective_payload(value),
    }


def signed_definition_from_dict(
    value: Mapping[str, Any],
) -> SignedAssetDefinition:
    return SignedAssetDefinition(
        id=str(value.get("id", "")),
        domain_id=str(value.get("domain_id", "")),
        name=str(value.get("name", "")),
        description=str(value.get("description", "")),
        content_type=str(value.get("content_type", "")),
        source_kind=str(value.get("source_kind", "")),
        source_uri=str(value.get("source_uri", "")),
        actor_id=str(value.get("actor_id", "")),
        key_id=str(value.get("key_id", "")),
        signature_kind=str(value.get("signature_kind", "")),
        signature=str(value.get("signature", "")),
        schema_version=value.get("schema_version", ASSET_GRAPH_SCHEMA_VERSION),
    )


def definition_content_assertion_to_dict(
    value: DefinitionContentAssertion,
) -> dict[str, Any]:
    return {
        "id": value.id,
        "signature": value.signature,
        **assertion_effective_payload(value),
    }


def definition_content_assertion_from_dict(
    value: Mapping[str, Any],
) -> DefinitionContentAssertion:
    return DefinitionContentAssertion(
        id=str(value.get("id", "")),
        domain_id=str(value.get("domain_id", "")),
        definition_id=str(value.get("definition_id", "")),
        content_id=str(value.get("content_id", "")),
        relation_type=str(value.get("relation_type", "")),
        actor_id=str(value.get("actor_id", "")),
        key_id=str(value.get("key_id", "")),
        signature_kind=str(value.get("signature_kind", "")),
        signature=str(value.get("signature", "")),
        evidence=value.get("evidence", {}),
        schema_version=value.get("schema_version", ASSET_GRAPH_SCHEMA_VERSION),
    )
