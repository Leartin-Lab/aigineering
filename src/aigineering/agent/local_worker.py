"""Operator-owned local Worker adapter for heterogeneous Fleets."""

from __future__ import annotations

from importlib import import_module
import re
from typing import TYPE_CHECKING

from aigineering.core.worker_routing import WorkerRegistration

if TYPE_CHECKING:
    from aigineering.protocol.types import Asset, Candidate, Contract


class LocalWorker:
    """Wrap a factory-created Worker with fixed operator routing metadata."""

    def __init__(self, worker, spec) -> None:
        if getattr(worker, "worker_id", None) != spec.worker_id:
            raise ValueError("local Worker factory returned a different worker_id")
        if not callable(getattr(worker, "invoke", None)):
            raise ValueError(
                "local Worker factory must return an invoke-capable Worker"
            )
        self._worker = worker
        self.worker_id = spec.worker_id
        self._spec = spec

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(
            self.worker_id,
            capabilities=self._spec.capabilities,
            pools=self._spec.pools,
            profile_id=self._spec.profile_id or "local-worker-v1",
            capacity=self._spec.capacity,
            version=self._spec.version,
        )

    def invoke(self, contract: Contract, disclosed_assets: list[Asset]) -> Candidate:
        return self._worker.invoke(contract, disclosed_assets)


def build_local_worker(factory_path: str, spec):
    """Import and invoke one explicitly configured operator factory."""
    module_name, separator, factory_name = factory_path.partition(":")
    if (
        not separator
        or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module_name)
        or not factory_name.isidentifier()
    ):
        raise ValueError("local worker_factory must be in 'module:factory' form")
    try:
        module = import_module(module_name)
    except (ImportError, OSError) as exc:
        raise ValueError(f"cannot import local Worker module {module_name!r}") from exc
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise ValueError(f"local Worker factory is not callable: {factory_path}")
    try:
        worker = factory(worker_id=spec.worker_id)
    except Exception as exc:
        raise ValueError("local Worker factory initialization failed") from exc
    return LocalWorker(worker, spec)
