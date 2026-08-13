"""Explicit local-code adapter for configured ToolRegistry factories."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

from aigineering.core.tools import ToolRegistry
from aigineering.protocol.immutability import deep_thaw


def load_tool_registry(factory_reference: str) -> ToolRegistry:
    """Load ``module:factory`` only when explicitly configured by the operator."""
    module_reference, separator, factory_name = factory_reference.rpartition(":")
    if not separator or not module_reference or not factory_name:
        raise ValueError("--tool-registry must use module:factory")
    module_path = Path(module_reference)
    if module_path.is_file():
        spec = importlib.util.spec_from_file_location(
            f"aigineering_tool_registry_{abs(hash(module_path.resolve()))}",
            module_path,
        )
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot load tool registry file {module_reference!r}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except (ImportError, OSError) as exc:
            raise ValueError(
                f"cannot load tool registry file {module_reference!r}"
            ) from exc
    else:
        try:
            module = importlib.import_module(module_reference)
        except ImportError as exc:
            raise ValueError(
                f"cannot import tool registry module {module_reference!r}"
            ) from exc
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise ValueError(f"tool registry factory {factory_reference!r} is not callable")
    try:
        registry = factory()
    except Exception as exc:
        raise ValueError(
            f"tool registry factory {factory_reference!r} failed: {exc}"
        ) from exc
    if not isinstance(registry, ToolRegistry):
        raise ValueError(
            f"tool registry factory {factory_reference!r} did not return ToolRegistry"
        )
    if not registry.list_specs():
        raise ValueError(
            f"tool registry factory {factory_reference!r} returned no tools"
        )
    return registry


def provider_tool_definitions(registry: ToolRegistry) -> list[dict[str, object]]:
    """Render public registry specs as OpenAI-compatible function definitions."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": deep_thaw(spec.input_schema),
            },
        }
        for spec in registry.list_specs()
    ]
