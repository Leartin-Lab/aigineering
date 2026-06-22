"""Core data models for the Aigineering agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Optional


@dataclass(frozen=True)
class Asset:
    id: str
    name: str
    content: str
    content_type: str = "text"
    created_by: str = ""
    origin: str = ""
    trust_tier: str = "untrusted"  # TrustTier enum value; see TrustTier.from_str()
    minted_by: str = ""
    source_uri: str = ""
    signed_by: str = ""
    provenance_seal: str = ""
    signer_kind: str = "deterministic"
    promptable: bool = True
    disclosure_view: str = "original"
    definition_hash: str = ""
    content_hash: str = ""
    keep_flag: bool = False
    tombstoned: bool = False
    tombstoned_at: Optional[str] = None
    lineage_id: str = ""


@dataclass(frozen=True)
class Contract:
    id: str
    parent_id: Optional[str] = None
    name: str = ""
    description: str = ""
    inputs: tuple[str, ...] = field(default_factory=tuple)
    outputs: tuple[str, ...] = field(default_factory=tuple)
    activation: str = ""
    budget: int = 0
    tool_scope: tuple[str, ...] = field(default_factory=tuple)
    labels: tuple[str, ...] = field(default_factory=tuple)
    origin: str = "human"
    minting_authority: tuple[str, ...] = field(default_factory=tuple)
    sensitive_input_policy: Optional[MappingProxyType] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "tool_scope", tuple(self.tool_scope))
        object.__setattr__(self, "labels", tuple(self.labels))
        object.__setattr__(self, "minting_authority", tuple(self.minting_authority))
        if self.sensitive_input_policy is not None:
            object.__setattr__(
                self,
                "sensitive_input_policy",
                MappingProxyType(dict(self.sensitive_input_policy)),
            )


@dataclass(frozen=True)
class Candidate:
    worker_id: str
    raw_output: str
    parsed_action: Optional[MappingProxyType] = None
    metadata: Optional[MappingProxyType] = None

    def __post_init__(self) -> None:
        if self.parsed_action is not None:
            object.__setattr__(
                self, "parsed_action", MappingProxyType(dict(self.parsed_action))
            )
        if self.metadata is not None:
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str = ""
    input_schema: MappingProxyType = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "input_schema", MappingProxyType(dict(self.input_schema))
        )


@dataclass(frozen=True)
class TraceEntry:
    id: str
    parent_id: Optional[str] = None
    contract_id: str = ""
    event_type: str = ""
    disclosed_assets: tuple[str, ...] = field(default_factory=tuple)
    worker_id: Optional[str] = None
    candidate_raw: Optional[str] = None
    accepted_fragments: tuple[str, ...] = field(default_factory=tuple)
    accepted_asset_names: tuple[str, ...] = field(default_factory=tuple)
    rejected_fragments: tuple[str, ...] = field(default_factory=tuple)
    authority_policy: Optional[str] = None
    authority_result: Optional[str] = None
    budget_remaining: int = 0
    relation_type: Optional[str] = None
    relation_target: Optional[str] = None
    timestamp: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "disclosed_assets", tuple(self.disclosed_assets))
        object.__setattr__(self, "accepted_fragments", tuple(self.accepted_fragments))
        object.__setattr__(
            self, "accepted_asset_names", tuple(self.accepted_asset_names)
        )
        object.__setattr__(self, "rejected_fragments", tuple(self.rejected_fragments))


class RejectionCategory(Enum):
    PARSE_ERROR = "parse_error"
    AUTHORITY_REJECTION = "authority_rejection"
    DUPLICATE_REJECTION = "duplicate_rejection"
    PROTECTED_NAME_REJECTION = "protected_name_rejection"


class ProjectionStatus(Enum):
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    REJECTED = "rejected"


# Legacy alias table — maps pre-unification tier names to canonical members.
_TRUST_TIER_LEGACY = {
    "low": 0,        # TrustTier.UNTRUSTED
    "medium": 2,     # TrustTier.CONFIGURED
    "high": 3,       # TrustTier.VERIFIED
    "worker": 0,     # TrustTier.UNTRUSTED
    "tool": 2,       # TrustTier.CONFIGURED
    "trusted": 3,    # TrustTier.VERIFIED
}


class TrustTier(Enum):
    """Canonical trust tier for assets and capability descriptors.

    Tiers are ordered by increasing trust: UNTRUSTED < OBSERVED < CONFIGURED
    < VERIFIED < SYSTEM < HUMAN.  The ordering is the integer ``.value``.

    Legacy tier names (``low``, ``medium``, ``high``, ``worker``, ``tool``,
    ``trusted``) are NOT enum members but are accepted by :meth:`from_str` for
    backward compatibility.

    Canonical members (G10 gate set):
        UNTRUSTED     = 0  (default for unverified content)
        OBSERVED      = 1  (passively observed, e.g. worker output)
        CONFIGURED    = 2  (explicitly configured, e.g. tool descriptor)
        VERIFIED      = 3  (verified by a trusted process)
        SYSTEM        = 4  (runtime-internal, e.g. method system assets)
        HUMAN         = 5  (human/control-plane attestation)
    """

    UNTRUSTED = 0
    OBSERVED = 1
    CONFIGURED = 2
    VERIFIED = 3
    SYSTEM = 4
    HUMAN = 5

    def __str__(self) -> str:
        return self.name.lower()

    def __repr__(self) -> str:
        return f"TrustTier.{self.name}"

    @classmethod
    def from_str(cls, value: str) -> "TrustTier":
        """Resolve a tier name string to a TrustTier member.

        Accepts both canonical names (``"untrusted"``, ``"observed"``, …)
        and legacy aliases (``"low"``, ``"medium"``, ``"high"``, ``"worker"``,
        ``"tool"``, ``"trusted"``).

        Raises:
            ValueError: if *value* is not a recognised tier name.
        """
        canonical = value.lower()
        try:
            return cls[canonical.upper()]
        except KeyError:
            pass
        mapped = _TRUST_TIER_LEGACY.get(canonical)
        if mapped is not None:
            return cls(mapped)
        raise ValueError(
            f"Unknown trust tier {value!r}. "
            f"Valid tiers: {[t.name.lower() for t in cls]}"
        )


@dataclass(frozen=True)
class RejectedCandidate:
    name: str
    content: str
    reject_reason: str
    category: RejectionCategory = RejectionCategory.AUTHORITY_REJECTION


@dataclass(frozen=True)
class ReplacementClaim:
    id: str
    source_asset_id: str
    replacement_asset_id: str
    definition_hash: str
    claim_type: str
    signed_by: str = ""
    provenance_seal: str = ""
    lineage_id: str = ""

    _VALID_CLAIM_TYPES = frozenset(
        {
            "replacement",
            "slice",
            "summary",
            "redaction",
            "equivalent_input",
        }
    )

    def __post_init__(self) -> None:
        if self.claim_type not in self._VALID_CLAIM_TYPES:
            raise ValueError(
                f"Invalid claim_type '{self.claim_type}'. "
                f"Must be one of: {sorted(self._VALID_CLAIM_TYPES)}"
            )


@dataclass(frozen=True)
class ProjectionResult:
    accepted_assets: tuple[Asset, ...] = field(default_factory=tuple)
    rejected_candidates: tuple[RejectedCandidate, ...] = field(default_factory=tuple)
    raw_candidate: str = ""
    status: ProjectionStatus = ProjectionStatus.REJECTED
    authority_policy: Optional[MappingProxyType] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "accepted_assets", tuple(self.accepted_assets))
        object.__setattr__(self, "rejected_candidates", tuple(self.rejected_candidates))
        if self.authority_policy is not None:
            object.__setattr__(
                self, "authority_policy", MappingProxyType(dict(self.authority_policy))
            )


@dataclass(frozen=True)
class Session:
    id: str
    root_contract_id: str = ""
    contract_ids: tuple[str, ...] = field(default_factory=tuple)
    asset_ids: tuple[str, ...] = field(default_factory=tuple)
    trace_ids: tuple[str, ...] = field(default_factory=tuple)
    config_snapshot: MappingProxyType = field(default_factory=dict)
    worker_snapshot: MappingProxyType = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "contract_ids", tuple(self.contract_ids))
        object.__setattr__(self, "asset_ids", tuple(self.asset_ids))
        object.__setattr__(self, "trace_ids", tuple(self.trace_ids))
        object.__setattr__(
            self, "config_snapshot", MappingProxyType(dict(self.config_snapshot))
        )
        object.__setattr__(
            self, "worker_snapshot", MappingProxyType(dict(self.worker_snapshot))
        )
