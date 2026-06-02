"""Core data models for the Aigineering agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Asset:
    id: str
    name: str
    content: str
    content_type: str = "text"
    created_by: str = ""
    origin: str = "system"


@dataclass(frozen=True)
class Contract:
    id: str
    parent_id: Optional[str] = None
    name: str = ""
    description: str = ""
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    activation: str = ""
    budget: int = 0
    tool_scope: list[str] = field(default_factory=list)
    origin: str = "human"


@dataclass(frozen=True)
class Candidate:
    worker_id: str
    raw_output: str
    parsed_action: Optional[dict] = None


@dataclass(frozen=True)
class TraceEntry:
    id: str
    parent_id: Optional[str] = None
    contract_id: str = ""
    event_type: str = ""
    disclosed_assets: list[str] = field(default_factory=list)
    worker_id: Optional[str] = None
    candidate_raw: Optional[str] = None
    accepted_fragments: list[str] = field(default_factory=list)
    rejected_fragments: list[str] = field(default_factory=list)
    authority_policy: Optional[str] = None
    authority_result: Optional[bool] = None
    budget_remaining: int = 0
    relation_type: Optional[str] = None
    relation_target: Optional[str] = None
    timestamp: str = ""
