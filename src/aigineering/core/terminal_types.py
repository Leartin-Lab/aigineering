"""Dependency-neutral canonical terminal event values."""

from __future__ import annotations

TERMINAL_EVENTS = frozenset({"complete", "failed", "cancelled", "unreachable"})

__all__ = ["TERMINAL_EVENTS"]
