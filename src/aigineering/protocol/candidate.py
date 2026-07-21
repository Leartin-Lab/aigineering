"""Authenticated, typed Candidate protocol values.

Receipt proves who proposed effects and that their bytes were not changed.  It
does not make an effect a runtime fact; commitment belongs to a separate reducer.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from aigineering.core.ids import canonical_json, compute_content_hash
from aigineering.core.signing import Signer, Verifier, create_verifier
from aigineering.protocol.immutability import deep_freeze, deep_thaw
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record


CANDIDATE_PROTOCOL_VERSION = 1
MAX_SAFE_JSON_INTEGER = (1 << 53) - 1


def _validate_signed_json(value: Any, *, path: str) -> None:
    """Restrict signed payloads to a language-neutral I-JSON value subset."""
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_JSON_INTEGER:
            raise ValueError(f"{path} integer exceeds the interoperable JSON range")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} object keys must be strings")
            _validate_signed_json(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_signed_json(item, path=f"{path}[{index}]")
        return
    if isinstance(value, float):
        raise ValueError(
            f"{path} floating-point values are not canonical; use a decimal string"
        )
    raise ValueError(
        f"{path} contains unsupported signed JSON value {type(value).__name__}"
    )


@dataclass(frozen=True)
class CandidateClaimBinding:
    """Exact claim/package fence for one pull-Worker Candidate."""

    contract_id: str
    claim_id: str
    claim_epoch: int
    package_id: str

    def __post_init__(self) -> None:
        for field_name in ("contract_id", "claim_id", "package_id"):
            if not getattr(self, field_name):
                raise ValueError(
                    f"CandidateClaimBinding.{field_name} must not be empty"
                )
        if type(self.claim_epoch) is not int or not (
            1 <= self.claim_epoch <= MAX_SAFE_JSON_INTEGER
        ):
            raise ValueError(
                "CandidateClaimBinding.claim_epoch must be a positive "
                "interoperable JSON integer"
            )
        if not self.package_id.startswith("pkg:"):
            raise ValueError("CandidateClaimBinding.package_id must start with 'pkg:'")


@dataclass(frozen=True)
class ActorKey:
    """One actor key authorized by a Genesis manifest."""

    actor_id: str
    key_id: str
    kind: str
    public_key: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    revoked: bool = False

    def __post_init__(self) -> None:
        for field_name in ("actor_id", "key_id", "kind", "public_key"):
            if not getattr(self, field_name):
                raise ValueError(f"ActorKey.{field_name} must not be empty")
        object.__setattr__(self, "capabilities", tuple(sorted(self.capabilities)))


@dataclass(frozen=True)
class GenesisManifest:
    """Immutable trust root for one Candidate domain."""

    id: str
    domain: str
    root_keys: tuple[ActorKey, ...]
    policy_hash: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.domain:
            raise ValueError("GenesisManifest.domain must not be empty")
        if not self.root_keys:
            raise ValueError("GenesisManifest.root_keys must not be empty")
        if not self.policy_hash:
            raise ValueError("GenesisManifest.policy_hash must not be empty")
        identities = [(key.actor_id, key.key_id) for key in self.root_keys]
        if len(identities) != len(set(identities)):
            raise ValueError("GenesisManifest contains duplicate actor/key identities")
        object.__setattr__(
            self,
            "root_keys",
            tuple(sorted(self.root_keys, key=lambda key: (key.actor_id, key.key_id))),
        )


def _actor_key_dict(key: ActorKey) -> dict[str, Any]:
    return {
        "actor_id": key.actor_id,
        "capabilities": list(key.capabilities),
        "key_id": key.key_id,
        "kind": key.kind,
        "public_key": key.public_key,
        "revoked": key.revoked,
    }


def genesis_effective_payload(manifest: GenesisManifest) -> dict[str, Any]:
    return {
        "domain": manifest.domain,
        "policy_hash": manifest.policy_hash,
        "root_keys": [_actor_key_dict(key) for key in manifest.root_keys],
        "schema_version": manifest.schema_version,
    }


def create_genesis_manifest(
    domain: str,
    root_keys: tuple[ActorKey, ...] | list[ActorKey],
    policy_hash: str,
    *,
    schema_version: int = 1,
) -> GenesisManifest:
    provisional = GenesisManifest(
        id="pending",
        domain=domain,
        root_keys=tuple(root_keys),
        policy_hash=policy_hash,
        schema_version=schema_version,
    )
    manifest_id = "genesis:" + compute_content_hash(
        canonical_json(genesis_effective_payload(provisional))
    )
    return replace(provisional, id=manifest_id)


def validate_genesis_manifest(manifest: GenesisManifest) -> None:
    expected = create_genesis_manifest(
        manifest.domain,
        manifest.root_keys,
        manifest.policy_hash,
        schema_version=manifest.schema_version,
    ).id
    if manifest.id != expected:
        raise ValueError(
            f"Genesis manifest id mismatch: supplied {manifest.id!r}, "
            f"expected {expected!r}"
        )


def genesis_manifest_to_dict(manifest: GenesisManifest) -> dict[str, Any]:
    """Serialize a Genesis manifest without weakening its canonical fields."""
    return {"id": manifest.id, **genesis_effective_payload(manifest)}


def genesis_manifest_from_dict(data: Mapping[str, Any]) -> GenesisManifest:
    keys = tuple(
        ActorKey(
            actor_id=str(item.get("actor_id", "")),
            key_id=str(item.get("key_id", "")),
            kind=str(item.get("kind", "")),
            public_key=str(item.get("public_key", "")),
            capabilities=tuple(item.get("capabilities", ())),
            revoked=bool(item.get("revoked", False)),
        )
        for item in data.get("root_keys", ())
    )
    manifest = GenesisManifest(
        id=str(data.get("id", "")),
        domain=str(data.get("domain", "")),
        root_keys=keys,
        policy_hash=str(data.get("policy_hash", "")),
        schema_version=int(data.get("schema_version", 1)),
    )
    validate_genesis_manifest(manifest)
    return manifest


@dataclass(frozen=True)
class CandidateEffect:
    """One proposed typed effect; its payload is recursively immutable."""

    effect_type: str
    payload: Mapping[str, Any]
    atomic_group: str = ""

    def __post_init__(self) -> None:
        if not self.effect_type or "." not in self.effect_type:
            raise ValueError(
                "effect_type must be a namespaced value such as asset.propose"
            )
        _validate_signed_json(self.payload, path="CandidateEffect.payload")
        object.__setattr__(self, "payload", deep_freeze(dict(self.payload)))


@dataclass(frozen=True)
class CandidateProposal:
    """Content-addressed and actor-signed collection of proposed effects."""

    id: str
    domain_id: str
    actor_id: str
    key_id: str
    signature_kind: str
    signature: str
    effects: tuple[CandidateEffect, ...]
    causal_parents: tuple[str, ...] = field(default_factory=tuple)
    idempotency_key: str = ""
    claim_binding: CandidateClaimBinding | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: int = CANDIDATE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("domain_id", "actor_id", "key_id", "signature_kind"):
            if not getattr(self, field_name):
                raise ValueError(f"CandidateProposal.{field_name} must not be empty")
        if not self.effects:
            raise ValueError("CandidateProposal.effects must not be empty")
        if (
            type(self.protocol_version) is not int
            or self.protocol_version != CANDIDATE_PROTOCOL_VERSION
        ):
            raise ValueError(
                f"unsupported Candidate protocol_version {self.protocol_version}"
            )
        _validate_signed_json(self.metadata, path="CandidateProposal.metadata")
        object.__setattr__(self, "effects", tuple(self.effects))
        object.__setattr__(self, "causal_parents", tuple(self.causal_parents))
        object.__setattr__(self, "metadata", deep_freeze(dict(self.metadata)))


def candidate_effective_payload(candidate: CandidateProposal) -> dict[str, Any]:
    """Return the bytes-bound payload, excluding ID and signature."""
    payload = {
        "actor_id": candidate.actor_id,
        "causal_parents": list(candidate.causal_parents),
        "domain_id": candidate.domain_id,
        "effects": [
            {
                "atomic_group": effect.atomic_group,
                "effect_type": effect.effect_type,
                "payload": deep_thaw(effect.payload),
            }
            for effect in candidate.effects
        ],
        "idempotency_key": candidate.idempotency_key,
        "key_id": candidate.key_id,
        "protocol_version": candidate.protocol_version,
        "signature_kind": candidate.signature_kind,
    }
    if candidate.claim_binding is not None:
        payload["claim_binding"] = {
            "claim_epoch": candidate.claim_binding.claim_epoch,
            "claim_id": candidate.claim_binding.claim_id,
            "contract_id": candidate.claim_binding.contract_id,
            "package_id": candidate.claim_binding.package_id,
        }
    if candidate.metadata:
        payload["metadata"] = deep_thaw(candidate.metadata)
    return payload


def candidate_signing_bytes(candidate: CandidateProposal) -> bytes:
    canonical = canonical_json(candidate_effective_payload(candidate))
    return unicodedata.normalize("NFC", canonical).encode("utf-8")


def candidate_content_id(candidate: CandidateProposal) -> str:
    """Return the v1 ID derived from exactly the bytes actors sign."""
    return (
        "candidate:v1:" + hashlib.sha256(candidate_signing_bytes(candidate)).hexdigest()
    )


def candidate_proposal_to_dict(candidate: CandidateProposal) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "signature": candidate.signature,
        **candidate_effective_payload(candidate),
    }


def candidate_proposal_from_dict(data: Mapping[str, Any]) -> CandidateProposal:
    effects = tuple(
        CandidateEffect(
            effect_type=str(item.get("effect_type", "")),
            payload=item.get("payload", {}),
            atomic_group=str(item.get("atomic_group", "")),
        )
        for item in data.get("effects", ())
    )
    raw_claim = data.get("claim_binding")
    claim_binding = None
    if raw_claim is not None:
        if not isinstance(raw_claim, Mapping):
            raise ValueError("Candidate claim_binding must be an object")
        claim_binding = CandidateClaimBinding(
            contract_id=str(raw_claim.get("contract_id", "")),
            claim_id=str(raw_claim.get("claim_id", "")),
            claim_epoch=raw_claim.get("claim_epoch", 0),
            package_id=str(raw_claim.get("package_id", "")),
        )
    return CandidateProposal(
        id=str(data.get("id", "")),
        domain_id=str(data.get("domain_id", "")),
        actor_id=str(data.get("actor_id", "")),
        key_id=str(data.get("key_id", "")),
        signature_kind=str(data.get("signature_kind", "")),
        signature=str(data.get("signature", "")),
        effects=effects,
        causal_parents=tuple(data.get("causal_parents", ())),
        idempotency_key=str(data.get("idempotency_key", "")),
        claim_binding=claim_binding,
        metadata=data.get("metadata", {}),
        protocol_version=data.get("protocol_version", CANDIDATE_PROTOCOL_VERSION),
    )


def create_candidate_proposal(
    *,
    domain_id: str,
    actor_id: str,
    key_id: str,
    effects: tuple[CandidateEffect, ...] | list[CandidateEffect],
    signer: Signer,
    causal_parents: tuple[str, ...] | list[str] = (),
    idempotency_key: str = "",
    claim_binding: CandidateClaimBinding | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> CandidateProposal:
    provisional = CandidateProposal(
        id="pending",
        domain_id=domain_id,
        actor_id=actor_id,
        key_id=key_id,
        signature_kind=signer.kind,
        signature="pending",
        effects=tuple(effects),
        causal_parents=tuple(causal_parents),
        idempotency_key=idempotency_key,
        claim_binding=claim_binding,
        metadata=metadata or {},
    )
    candidate_id = candidate_content_id(provisional)
    identified = replace(provisional, id=candidate_id)
    return replace(
        identified, signature=signer.sign(candidate_signing_bytes(identified))
    )


VerifierFactory = Callable[[str], Verifier]


def verify_candidate_proposal(
    candidate: CandidateProposal,
    genesis: GenesisManifest,
    *,
    verifier_factory: VerifierFactory = create_verifier,
    actor_keys: tuple[ActorKey, ...] | None = None,
) -> None:
    """Fail closed unless identity, domain, bytes, and signature all match."""
    validate_genesis_manifest(genesis)
    if candidate.claim_binding is not None and not candidate.idempotency_key:
        raise ValueError("claim-bound Candidate.idempotency_key must not be empty")
    if candidate.domain_id != genesis.id:
        raise ValueError("Candidate domain does not match Genesis manifest")
    effective_keys = actor_keys if actor_keys is not None else genesis.root_keys
    matching = [
        key
        for key in effective_keys
        if key.actor_id == candidate.actor_id and key.key_id == candidate.key_id
    ]
    if len(matching) != 1:
        raise ValueError("Candidate actor/key is not authorized by Genesis")
    key = matching[0]
    if key.revoked:
        raise ValueError("Candidate actor key is revoked")
    if key.kind != candidate.signature_kind:
        raise ValueError("Candidate signature kind does not match actor key")
    if candidate.signature_kind in {"deterministic", "asig_"}:
        raise ValueError(
            "Deterministic content seals cannot authenticate Candidate actors"
        )
    expected_id = candidate_content_id(candidate)
    if candidate.id != expected_id:
        raise ValueError("Candidate content id does not match effective payload")
    verifier = verifier_factory(candidate.signature_kind)
    if not verifier.verify(
        candidate_signing_bytes(candidate), candidate.signature, key.public_key
    ):
        raise ValueError("Candidate signature verification failed")


def candidate_received_record(
    candidate: CandidateProposal,
    genesis: GenesisManifest,
    *,
    verifier_factory: VerifierFactory = create_verifier,
    actor_keys: tuple[ActorKey, ...] | None = None,
) -> RuntimeRecord:
    """Verify a Candidate and represent receipt without accepting its effects."""
    verify_candidate_proposal(
        candidate,
        genesis,
        verifier_factory=verifier_factory,
        actor_keys=actor_keys,
    )
    return create_runtime_record(
        "candidate.received",
        {
            "actor_id": candidate.actor_id,
            "candidate_id": candidate.id,
            "domain_id": candidate.domain_id,
            "effect_types": [effect.effect_type for effect in candidate.effects],
            "key_id": candidate.key_id,
            "metadata": deep_thaw(candidate.metadata),
            "signature": candidate.signature,
            "signature_kind": candidate.signature_kind,
        },
        causal_parents=candidate.causal_parents,
    )
