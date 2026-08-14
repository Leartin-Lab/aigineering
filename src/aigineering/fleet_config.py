"""Declarative local Worker fleet configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib

from aigineering.agent.llm import LLMWorker
from aigineering.agent.tool_registry_loader import (
    load_tool_registry,
    provider_tool_definitions,
)
from aigineering.agent.tool_worker import ToolWorker


@dataclass(frozen=True)
class FleetWorkerSpec:
    worker_id: str
    kind: str
    capacity: int = 1
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "AIGINEERING_API_KEY"
    capabilities: tuple[str, ...] = ()
    pools: tuple[str, ...] = ()
    provider_capabilities: tuple[str, ...] = ()
    profile_id: str = ""
    tool_registry: str = ""
    timeout: float = 60.0
    max_retries: int = 3
    max_output_tokens: int = 2048
    thinking_mode: str = ""
    effect_capabilities: tuple[str, ...] = ()
    version: str = "1"

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("fleet worker id must not be empty")
        if self.kind not in {"llm", "tool"}:
            raise ValueError("fleet worker kind must be 'llm' or 'tool'")
        if self.capacity < 1:
            raise ValueError("fleet worker capacity must be at least 1")
        if self.kind == "llm" and not self.model:
            raise ValueError("fleet LLM worker requires model")
        if self.kind == "tool" and not self.tool_registry:
            raise ValueError("fleet tool worker requires tool_registry")
        if self.max_output_tokens < 1:
            raise ValueError("fleet max_output_tokens must be at least 1")
        if self.thinking_mode not in {"", "enabled", "disabled"}:
            raise ValueError(
                "fleet thinking_mode must be 'enabled', 'disabled', or empty"
            )


@dataclass(frozen=True)
class LocalFleetConfig:
    db_path: str
    poll_interval: float
    workers: tuple[FleetWorkerSpec, ...]


def load_fleet_config(path: str | Path) -> LocalFleetConfig:
    """Parse one strict TOML fleet description without resolving secrets."""
    selected = Path(path)
    data = tomllib.loads(selected.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("fleet config must be a TOML table")
    allowed_root = {"fleet", "workers"}
    unknown_root = set(data) - allowed_root
    if unknown_root:
        raise ValueError(f"unknown fleet config fields: {sorted(unknown_root)}")
    fleet = data.get("fleet", {})
    if not isinstance(fleet, dict):
        raise ValueError("[fleet] must be a TOML table")
    unknown_fleet = set(fleet) - {"db_path", "poll_interval"}
    if unknown_fleet:
        raise ValueError(f"unknown [fleet] fields: {sorted(unknown_fleet)}")
    raw_workers = data.get("workers", [])
    if not isinstance(raw_workers, list) or not raw_workers:
        raise ValueError("fleet config requires at least one [[workers]] entry")
    workers = tuple(_worker_spec(item) for item in raw_workers)
    ids = [worker.worker_id for worker in workers]
    if len(ids) != len(set(ids)):
        raise ValueError("fleet worker ids must be unique")
    poll_interval = float(fleet.get("poll_interval", 0.1))
    if poll_interval <= 0:
        raise ValueError("fleet poll_interval must be positive")
    return LocalFleetConfig(
        db_path=str(fleet.get("db_path", ".aig/store.db")),
        poll_interval=poll_interval,
        workers=workers,
    )


def build_fleet_worker(spec: FleetWorkerSpec):
    """Build one stateless Worker adapter from an operator-owned profile."""
    registry = load_tool_registry(spec.tool_registry) if spec.tool_registry else None
    if spec.kind == "tool":
        assert registry is not None
        return ToolWorker(
            registry,
            worker_id=spec.worker_id,
            pools=spec.pools,
            capacity=spec.capacity,
            profile_id=spec.profile_id or "tool-worker-v1",
            routing_capabilities=spec.capabilities,
            registration_version=spec.version,
        )
    return LLMWorker(
        model=spec.model,
        api_key=os.environ.get(spec.api_key_env) if spec.api_key_env else None,
        base_url=spec.base_url,
        worker_id=spec.worker_id,
        timeout=int(spec.timeout),
        max_retries=spec.max_retries,
        max_output_tokens=spec.max_output_tokens,
        thinking_mode=spec.thinking_mode,
        capabilities=frozenset(spec.provider_capabilities),
        tool_definitions=(
            provider_tool_definitions(registry) if registry is not None else None
        ),
        routing_capabilities=frozenset(spec.capabilities),
        worker_pools=frozenset(spec.pools),
        profile_id=spec.profile_id or "openai-compatible-v1",
        capacity=spec.capacity,
        registration_version=spec.version,
    )


def _worker_spec(raw: object) -> FleetWorkerSpec:
    if not isinstance(raw, dict):
        raise ValueError("each [[workers]] entry must be a TOML table")
    allowed = {
        "id",
        "kind",
        "capacity",
        "model",
        "base_url",
        "api_key_env",
        "capabilities",
        "pools",
        "provider_capabilities",
        "profile_id",
        "tool_registry",
        "timeout",
        "max_retries",
        "max_output_tokens",
        "thinking_mode",
        "effect_capabilities",
        "version",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown worker fields: {sorted(unknown)}")
    for name in (
        "capabilities",
        "pools",
        "provider_capabilities",
        "effect_capabilities",
    ):
        value = raw.get(name, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError(f"worker {name} must be a list of strings")
    return FleetWorkerSpec(
        worker_id=str(raw.get("id", "")),
        kind=str(raw.get("kind", "")),
        capacity=int(raw.get("capacity", 1)),
        model=str(raw.get("model", "")),
        base_url=str(raw.get("base_url", "https://api.openai.com/v1")),
        api_key_env=str(raw.get("api_key_env", "AIGINEERING_API_KEY")),
        capabilities=tuple(raw.get("capabilities", ())),
        pools=tuple(raw.get("pools", ())),
        provider_capabilities=tuple(raw.get("provider_capabilities", ())),
        profile_id=str(raw.get("profile_id", "")),
        tool_registry=str(raw.get("tool_registry", "")),
        timeout=float(raw.get("timeout", 60.0)),
        max_retries=int(raw.get("max_retries", 3)),
        max_output_tokens=int(raw.get("max_output_tokens", 2048)),
        thinking_mode=str(raw.get("thinking_mode", "")),
        effect_capabilities=tuple(raw.get("effect_capabilities", ())),
        version=str(raw.get("version", "1")),
    )
