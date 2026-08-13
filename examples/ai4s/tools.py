"""Configured tools for the auditable AI4S literature example."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any

from aigineering.core.tools import ToolRegistry
from aigineering.protocol.types import ToolSpec

_ADAPTER = (
    Path(__file__).parents[1] / "literature-evidence" / "scripts" / "openalex_search.py"
)


def build_registry() -> ToolRegistry:
    """Return the explicit local registry consumed by ``aig run``."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="openalex_search",
            description=(
                "Search OpenAlex for a bounded set of scholarly works. "
                "Returns stable work IDs and retrieval provenance as JSON."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "max_records": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 25,
                    },
                    "from_year": {"type": "integer", "minimum": 1},
                    "to_year": {"type": "integer", "minimum": 1},
                },
            },
        ),
        _openalex_search,
    )
    return registry


def _openalex_search(args: dict[str, Any]) -> str:
    adapter = _load_adapter()
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    max_records = _bounded_int(args.get("max_records", 5), "max_records", 1, 25)
    from_year = _optional_year(args.get("from_year"), "from_year")
    to_year = _optional_year(args.get("to_year"), "to_year")
    if from_year is not None and to_year is not None and from_year > to_year:
        raise ValueError("from_year must not be later than to_year")
    filters: dict[str, str] = {}
    if from_year is not None:
        filters["from_publication_date"] = f"{from_year:04d}-01-01"
    if to_year is not None:
        filters["to_publication_date"] = f"{to_year:04d}-12-31"
    fixture = os.environ.get("AIGINEERING_AI4S_OPENALEX_FIXTURE")
    if fixture:
        payload = json.loads(Path(fixture).read_text(encoding="utf-8"))
    else:
        payload = adapter.fetch_response(
            query=query.strip(),
            max_records=max_records,
            api_key=os.environ.get("OPENALEX_API_KEY"),
            timeout=20.0,
            filters=filters,
        )
    manifest = adapter.normalize_response(
        payload,
        query=query.strip(),
        max_records=max_records,
        retrieved_at=os.environ.get("AIGINEERING_AI4S_RETRIEVED_AT")
        or datetime.now(timezone.utc).isoformat(),
        filters=filters,
    )
    return json.dumps(manifest, sort_keys=True, ensure_ascii=False)


def _load_adapter() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "aigineering_example_openalex_search", _ADAPTER
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("OpenAlex example adapter could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bounded_int(value: object, name: str, low: int, high: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def _optional_year(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _bounded_int(value, name, 1, 9999)
