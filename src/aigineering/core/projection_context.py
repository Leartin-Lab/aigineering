"""Build immutable reducer input snapshots from a StorePort."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aigineering.protocol.runtime_record import RuntimeRecord
from aigineering.protocol.candidate import ActorKey
from aigineering.protocol.types import Asset, Contract

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol


@dataclass(frozen=True)
class EffectProjectionContext:
    """Immutable fact snapshot required by reference-validating effects."""

    contracts: tuple[Contract, ...] = ()
    assets: tuple[Asset, ...] = ()
    runtime_records: tuple[RuntimeRecord, ...] = ()
    actor_keys: tuple[ActorKey, ...] = ()


def load_effect_projection_context(
    store: StoreProtocol, *, actor_keys: tuple[ActorKey, ...] = ()
) -> EffectProjectionContext:
    """Read facts once; typed effect projection remains a pure function."""
    return EffectProjectionContext(
        contracts=tuple(store.get_all_contracts()),
        assets=tuple(store.get_all_assets()),
        runtime_records=tuple(record for _, record in store.scan_runtime_records()),
        actor_keys=actor_keys,
    )
