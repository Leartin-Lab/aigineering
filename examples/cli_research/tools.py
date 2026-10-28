"""Bounded deterministic tools; runtime owns all planning and recovery.

The optional failure marker simulates one backend outage for a private test.
It is never consulted for task readiness, acceptance or recovery publication.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from aigineering.core.tools import ToolRegistry
from aigineering.protocol.types import ToolSpec

SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["success", "count", "total_c", "mean_c", "digest"],
    "properties": {
        "success": {"type": "boolean", "const": True},
        "count": {"type": "integer", "minimum": 1, "maximum": 32},
        "total_c": {"type": "number"},
        "mean_c": {"type": "number"},
        "digest": {"type": "string", "minLength": 64, "maxLength": 64},
    },
}


def _encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _summary_digest(count: int, total: float, mean: float) -> str:
    return hashlib.sha256(_encode([count, total, mean]).encode()).hexdigest()


def _summarize(args) -> str:
    marker = os.environ.get("AIG_MEASUREMENT_FAIL_ONCE_FILE")
    if marker:
        try:
            with Path(marker).open("x", encoding="utf-8") as stream:
                stream.write("simulated backend failure\n")
        except FileExistsError:
            pass
        else:
            # The explicit output schema rejects this backend envelope; arbitrary
            # business JSON is never interpreted as a universal success policy.
            return _encode({"success": False, "error": "simulated_unavailable"})
    values = args["values"]
    total = math.fsum(values)
    mean = total / len(values)
    return _encode(
        {
            "success": True,
            "count": len(values),
            "total_c": total,
            "mean_c": mean,
            "digest": _summary_digest(len(values), total, mean),
        }
    )


def _convert(args) -> str:
    summary = args["summary"]
    count = summary["count"]
    total, mean = float(summary["total_c"]), float(summary["mean_c"])
    if not math.isclose(total / count, mean, rel_tol=1e-12, abs_tol=1e-12) or summary[
        "digest"
    ] != _summary_digest(count, total, mean):
        raise ValueError("summary values or digest do not match")
    return _encode(
        {"success": True, "mean_f": mean * 9 / 5 + 32, "digest": summary["digest"]}
    )


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="summarize_sample",
            description="Compute count, total_c, mean_c and a digest from sample Celsius values.",
            version="1.0.0",
            input_schema={
                "type": "object",
                "required": ["values"],
                "additionalProperties": False,
                "properties": {
                    "values": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 32,
                        "items": {
                            "type": "number",
                            "minimum": -273.15,
                            "maximum": 1000,
                        },
                    }
                },
            },
            output_schema=SUMMARY_SCHEMA,
            max_output_bytes=4096,
        ),
        _summarize,
    )
    registry.register(
        ToolSpec(
            name="convert_mean",
            description="Convert a complete summarize_sample result to Fahrenheit; copy its numeric values and digest exactly.",
            version="1.0.0",
            input_schema={
                "type": "object",
                "required": ["summary"],
                "additionalProperties": False,
                "properties": {"summary": SUMMARY_SCHEMA},
            },
            output_schema={
                "type": "object",
                "required": ["success", "mean_f", "digest"],
                "additionalProperties": False,
                "properties": {
                    "success": {"type": "boolean", "const": True},
                    "mean_f": {"type": "number"},
                    "digest": {"type": "string", "minLength": 64, "maxLength": 64},
                },
            },
            max_output_bytes=4096,
        ),
        _convert,
    )
    return registry
