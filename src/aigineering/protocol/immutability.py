"""Recursive freeze/thaw helpers for immutable protocol values."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


def deep_freeze(value: Any) -> Any:
    """Return an immutable recursive copy of a JSON-like value."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((deep_freeze(item) for item in value), key=repr))
    return value


def deep_thaw(value: Any) -> Any:
    """Return a mutable JSON-serializable recursive copy."""
    if isinstance(value, Mapping):
        return {str(key): deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [deep_thaw(item) for item in value]
    return value
