"""Small public protocol for plugins that propose ordinary Candidate effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable

from aigineering.protocol.candidate import CandidateEffect
from aigineering.protocol.immutability import deep_freeze
from aigineering.protocol.types import Asset, Contract


@dataclass(frozen=True)
class PluginRequest:
    """Disclosure-bounded input supplied to one task-producing plugin."""

    parent: Contract
    source: Contract | None = None
    assets: tuple[Asset, ...] = ()
    allowed_input_names: frozenset[str] = field(default_factory=frozenset)
    allowance: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "assets", tuple(self.assets))
        object.__setattr__(
            self, "allowed_input_names", frozenset(self.allowed_input_names)
        )
        if self.allowance < 0:
            raise ValueError("PluginRequest.allowance must not be negative")


@dataclass(frozen=True)
class PluginProposal:
    """Uncommitted effects and visible policy notes produced by a plugin."""

    effects: tuple[CandidateEffect, ...] = ()
    rejections: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "effects", tuple(self.effects))
        object.__setattr__(
            self,
            "rejections",
            tuple(deep_freeze(dict(item)) for item in self.rejections),
        )


@runtime_checkable
class TaskPlugin(Protocol):
    """Pure adapter from disclosed inputs to ordinary Candidate effects."""

    plugin_id: str

    def propose(self, request: PluginRequest) -> PluginProposal: ...
