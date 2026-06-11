"""Core data models for the Aigineering agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Optional


@dataclass(frozen=True)
class Asset:
    id: str
    name: str
    content: str
    content_type: str = "text"
    created_by: str = ""
    origin: str = "system"
    trust_tier: str = "untrusted"
    minted_by: str = ""
    source_uri: str = ""
    signed_by: str = ""
    signature: str = ""
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
    sensitive_input_policy: Optional[MappingProxyType] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "tool_scope", tuple(self.tool_scope))
        object.__setattr__(self, "labels", tuple(self.labels))
        if self.sensitive_input_policy is not None:
            object.__setattr__(
                self, "sensitive_input_policy",
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
            object.__setattr__(self, "parsed_action", MappingProxyType(dict(self.parsed_action)))
        if self.metadata is not None:
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str = ""
    input_schema: MappingProxyType = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))


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
        object.__setattr__(self, "accepted_asset_names", tuple(self.accepted_asset_names))
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
    signature: str = ""
    lineage_id: str = ""

    _VALID_CLAIM_TYPES = frozenset({
        "replacement", "slice", "summary", "redaction", "equivalent_input",
    })

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
            object.__setattr__(self, "authority_policy", MappingProxyType(dict(self.authority_policy)))


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
        object.__setattr__(self, "config_snapshot", MappingProxyType(dict(self.config_snapshot)))
        object.__setattr__(self, "worker_snapshot", MappingProxyType(dict(self.worker_snapshot)))
