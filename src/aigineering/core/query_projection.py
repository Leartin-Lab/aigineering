"""Read-only projections derived from an authoritative Store."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from aigineering.core.ids import canonical_json, compute_content_hash
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import (
    asset_to_dict,
    contract_from_dict,
    contract_to_dict,
)

QUERY_PROJECTION_SCHEMA = "v2"


class QueryProjection(Protocol):
    """Read surface that cannot mutate authoritative runtime facts."""

    def get_asset(self, asset_id: str) -> Asset | None: ...
    def get_assets_by_name(self, name: str) -> list[Asset]: ...
    def get_assets_by_definition(self, definition_hash: str) -> list[Asset]: ...
    def get_all_assets(self) -> list[Asset]: ...
    def get_contract(self, contract_id: str) -> Contract | None: ...
    def get_all_contracts(self) -> list[Contract]: ...
    def get_content_objects(self) -> list[dict]: ...
    def get_asset_definitions(self) -> list[dict]: ...
    def get_definition_content_assertions(
        self, *, definition_id: str = "", content_id: str = ""
    ) -> list[dict]: ...
    def memoize_json(
        self, view_name: str, identity: str, build: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]: ...


class StoreQueryProjection:
    """Authoritative fallback using the Store's existing read surface."""

    def __init__(
        self,
        store,
        *,
        redis_configured: bool = False,
        reason: str = "",
    ) -> None:
        self._store = store
        self._redis_configured = redis_configured
        self._reason = reason

    def get_asset(self, asset_id: str) -> Asset | None:
        return self._store.get_asset(asset_id)

    def get_assets_by_name(self, name: str) -> list[Asset]:
        return self._store.get_assets_by_name(name)

    def get_assets_by_definition(self, definition_hash: str) -> list[Asset]:
        return self._store.get_assets_by_definition(definition_hash)

    def get_all_assets(self) -> list[Asset]:
        return self._store.get_all_assets()

    def get_contract(self, contract_id: str) -> Contract | None:
        return self._store.get_contract(contract_id)

    def get_all_contracts(self) -> list[Contract]:
        return self._store.get_all_contracts()

    def get_content_objects(self) -> list[dict]:
        return self._store.get_content_objects()

    def get_asset_definitions(self) -> list[dict]:
        return self._store.get_asset_definitions()

    def get_definition_content_assertions(
        self, *, definition_id: str = "", content_id: str = ""
    ) -> list[dict]:
        return self._store.get_definition_content_assertions(
            definition_id=definition_id, content_id=content_id
        )

    def memoize_json(
        self, view_name: str, identity: str, build: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        del view_name, identity
        return build()

    def status(self) -> dict[str, object]:
        revision = (
            int(self._store.get_runtime_revision())
            if hasattr(self._store, "get_runtime_revision")
            else 0
        )
        return {
            "authoritative_revision": revision,
            "available": True,
            "backend": "sqlite",
            "configured": self._redis_configured,
            "current": True,
            "reason": self._reason,
        }


@dataclass(frozen=True)
class QuerySnapshot:
    """Canonical immutable payload used to build any disposable query cache."""

    domain_id: str
    revision: int
    assets: tuple[tuple[str, str], ...]
    contracts: tuple[tuple[str, str], ...]
    asset_names: tuple[tuple[str, tuple[str, ...]], ...]
    asset_definitions: tuple[tuple[str, tuple[str, ...]], ...]
    contents: tuple[tuple[str, str], ...]
    signed_definitions: tuple[tuple[str, str], ...]
    assertions: tuple[tuple[str, str], ...]
    digest: str


def _wire_json(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def build_query_snapshot(store, *, domain_id: str) -> QuerySnapshot:
    """Build one deterministic read snapshot from authoritative Store facts."""
    assets = sorted(store.get_all_assets(), key=lambda asset: asset.id)
    contracts = sorted(store.get_all_contracts(), key=lambda contract: contract.id)
    asset_rows = tuple((asset.id, _wire_json(asset_to_dict(asset))) for asset in assets)
    contract_rows = tuple(
        (contract.id, _wire_json(contract_to_dict(contract))) for contract in contracts
    )
    content_rows = tuple(
        (str(value["id"]), _wire_json(value)) for value in store.get_content_objects()
    )
    signed_definition_rows = tuple(
        (str(value["id"]), _wire_json(value)) for value in store.get_asset_definitions()
    )
    assertion_rows = tuple(
        (str(value["id"]), _wire_json(value))
        for value in store.get_definition_content_assertions()
    )

    names: dict[str, list[str]] = {}
    definitions: dict[str, list[str]] = {}
    for asset in assets:
        names.setdefault(asset.name, []).append(asset.id)
        definitions.setdefault(asset.definition_hash, []).append(asset.id)
    name_rows = tuple(
        (name, tuple(sorted(asset_ids))) for name, asset_ids in sorted(names.items())
    )
    definition_rows = tuple(
        (definition, tuple(sorted(asset_ids)))
        for definition, asset_ids in sorted(definitions.items())
    )
    revision = int(store.get_runtime_revision())
    payload = {
        "asset_definitions": definition_rows,
        "asset_names": name_rows,
        "assets": asset_rows,
        "contracts": contract_rows,
        "contents": content_rows,
        "signed_definitions": signed_definition_rows,
        "assertions": assertion_rows,
        "domain_id": domain_id,
        "revision": revision,
        "schema": QUERY_PROJECTION_SCHEMA,
    }
    return QuerySnapshot(
        domain_id=domain_id,
        revision=revision,
        assets=asset_rows,
        contracts=contract_rows,
        asset_names=name_rows,
        asset_definitions=definition_rows,
        contents=content_rows,
        signed_definitions=signed_definition_rows,
        assertions=assertion_rows,
        digest=compute_content_hash(canonical_json(payload)),
    )


def asset_from_query_json(payload: str) -> Asset:
    return Asset(**json.loads(payload))


def contract_from_query_json(payload: str) -> Contract:
    return contract_from_dict(json.loads(payload))
