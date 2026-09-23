"""Canonical, bounded usage metadata shared by Workers and projections."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_USAGE_COUNT = (1 << 53) - 1
TOKEN_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")

# Field order is part of deterministic serialization. These are the usage
# fields emitted by the built-in LLM/tool adapters and the existing harness
# adapters; arbitrary adapter payloads do not belong in usage metadata.
_FIELD_ORDER = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "model",
    "provider",
    "worker_profile",
    "contract_id",
    "tool",
    "tool_version",
    "duration_ms",
    "result_bytes",
    "error_type",
    "retryable",
    "adapter",
    "network",
)
_TOKEN_FIELDS = frozenset(TOKEN_USAGE_FIELDS)
_IDENTITY_FIELDS = frozenset(
    {
        "model",
        "provider",
        "worker_profile",
        "contract_id",
        "tool",
        "tool_version",
        "error_type",
        "adapter",
    }
)
_COUNT_FIELDS = frozenset({"duration_ms", "result_bytes"})


def _valid_value(key: str, value: object) -> bool:
    if key in _TOKEN_FIELDS | _COUNT_FIELDS:
        return valid_usage_count(value)
    if key in _IDENTITY_FIELDS:
        if (
            not isinstance(value, str)
            or (not value and key != "error_type")
            or len(value) > 256
        ):
            return False
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True
    if key == "retryable" or key == "network":
        return type(value) is bool
    return False


def valid_usage_count(value: object) -> bool:
    """Whether a value is a canonical interoperable non-negative count."""
    return type(value) is int and 0 <= value <= MAX_USAGE_COUNT


def normalize_usage_metadata(metadata: object) -> dict[str, Any] | None:
    """Filter a local adapter mapping to known, valid fields in stable order.

    ``None`` means usage was absent. An explicitly supplied empty mapping, or
    a mapping whose values were all filtered, remains ``{}``.
    """
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        return None
    return {
        key: metadata[key]
        for key in _FIELD_ORDER
        if key in metadata and _valid_value(key, metadata[key])
    }


def validate_usage_metadata(metadata: object) -> dict[str, Any] | None:
    """Validate a signed/envelope mapping without changing its semantic data."""
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise ValueError("usage_metadata must be a JSON object")
    if any(not isinstance(key, str) for key in metadata):
        raise ValueError("usage_metadata keys must be strings")
    unknown = set(metadata) - set(_FIELD_ORDER)
    if unknown:
        raise ValueError("usage_metadata contains unknown fields")
    for key, value in metadata.items():
        if not isinstance(key, str) or not _valid_value(key, value):
            raise ValueError("usage_metadata contains an invalid field value")
    return dict(metadata)
