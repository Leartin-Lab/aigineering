"""Bounded compile diagnostics shared by failure writers and proof readers.

These values describe a rejected plan. They never authorize work or select a
recovery policy. Version 1 uses compact, sorted, ASCII JSON for byte limits.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

MAX_COMPILE_DIAGNOSTIC_BYTES = 8192
MAX_COMPILE_REJECTIONS = 16
MAX_COMPILE_FIELD_BYTES = 512
_MAX_INTEGER = 2**53 - 1
_TEXT_FIELDS = frozenset(
    {"child_name", "field", "reason", "action", "expected", "actual"}
)
_INTEGER_FIELDS = frozenset({"requested", "effective", "remaining"})
_FIELDS = _TEXT_FIELDS | _INTEGER_FIELDS | {"recoverable"}


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _valid_value(key: str, value: object) -> bool:
    if key in _TEXT_FIELDS:
        if not isinstance(value, str):
            return False
        try:
            value.encode("utf-8")
        except UnicodeError:
            return False
        return True
    if key == "recoverable":
        return type(value) is bool
    if key in _INTEGER_FIELDS:
        return (key == "remaining" and value is None) or (
            type(value) is int and 0 <= value <= _MAX_INTEGER
        )
    return False


def _bounded_text(value: str) -> str:
    # At most 512 code points can fit the 512-byte ASCII-JSON string limit.
    result = value[:MAX_COMPILE_FIELD_BYTES]
    while len(_encoded(result)) > MAX_COMPILE_FIELD_BYTES:
        result = result[:-1]
    return result


def build_compile_diagnostics(rejections) -> dict | None:
    """Preserve a prefix of valid fields, making every truncation explicit.

    Invalid adapter diagnostics retain the existing textual failure path. No
    malformed or empty optional value is written as structured evidence.
    """
    if not isinstance(rejections, (list, tuple)) or not rejections:
        return None
    result = {
        "version": 1,
        "original_count": len(rejections),
        "omitted_count": len(rejections),
        "entries": [],
    }
    for source in rejections[:MAX_COMPILE_REJECTIONS]:
        if (
            not isinstance(source, Mapping)
            or not source
            or not set(source) <= _FIELDS
            or any(not _valid_value(key, value) for key, value in source.items())
        ):
            return None
        values = {
            key: _bounded_text(value) if key in _TEXT_FIELDS else value
            for key, value in source.items()
        }
        entry = {
            "values": values,
            "truncated_fields": sorted(
                key
                for key in _TEXT_FIELDS & source.keys()
                if values[key] != source[key]
            ),
        }
        result["entries"].append(entry)
        result["omitted_count"] -= 1
        if len(_encoded(result)) > MAX_COMPILE_DIAGNOSTIC_BYTES:
            result["entries"].pop()
            result["omitted_count"] += 1
            break
    return result if valid_compile_diagnostics(result) else None


def valid_compile_diagnostics(value: object) -> bool:
    """Validate the exact optional durable format, including frozen containers."""
    if not isinstance(value, Mapping) or set(value) != {
        "version",
        "original_count",
        "omitted_count",
        "entries",
    }:
        return False
    if type(value["version"]) is not int or value["version"] != 1:
        return False
    original, omitted, entries = (
        value["original_count"],
        value["omitted_count"],
        value["entries"],
    )
    if (
        type(original) is not int
        or not 1 <= original <= _MAX_INTEGER
        or type(omitted) is not int
        or not isinstance(entries, (list, tuple))
        or not 1 <= len(entries) <= MAX_COMPILE_REJECTIONS
        or omitted != original - len(entries)
        or omitted < 0
    ):
        return False
    plain_entries = []
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {
            "values",
            "truncated_fields",
        }:
            return False
        fields, truncated = entry["values"], entry["truncated_fields"]
        if (
            not isinstance(fields, Mapping)
            or not fields
            or not set(fields) <= _FIELDS
            or any(not _valid_value(key, item) for key, item in fields.items())
            or not isinstance(truncated, (list, tuple))
            or any(not isinstance(key, str) for key in truncated)
            or tuple(truncated) != tuple(sorted(set(truncated)))
            or not set(truncated) <= _TEXT_FIELDS & fields.keys()
            or any(
                len(_encoded(item)) > MAX_COMPILE_FIELD_BYTES
                for key, item in fields.items()
                if key in _TEXT_FIELDS
            )
        ):
            return False
        plain_entries.append(
            {"values": dict(fields), "truncated_fields": list(truncated)}
        )
    plain = dict(value, entries=plain_entries)
    return len(_encoded(plain)) <= MAX_COMPILE_DIAGNOSTIC_BYTES


def is_compile_failure(category: object) -> bool:
    return isinstance(category, str) and category.startswith(
        "worker_error:planning_compile_rejected:"
    )
