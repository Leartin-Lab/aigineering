"""In-memory and persistent key-value stores for Assets and Contracts."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from aigineering.core.authority import (
    RESERVED_PREFIXES,
    ReservedNamespaceError,
    _is_protected_name,
)
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.ids import compute_content_hash
from aigineering.core.worker_routing import (
    registration_from_record,
    worker_registration_record,
)
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    runtime_record_effective_payload,
    validate_runtime_record,
)
from aigineering.protocol.wire import asset_to_dict, contract_to_dict

_logger = logging.getLogger(__name__)


@runtime_checkable
class StoreProtocol(Protocol):
    """Protocol that any store (in-memory or persistent) must satisfy."""

    def add_asset(self, asset: Asset) -> None: ...
    def get_asset(self, asset_id: str) -> Optional[Asset]: ...
    def get_assets_by_name(self, name: str) -> list[Asset]: ...
    def has_asset_named(self, name: str) -> bool: ...
    def get_all_assets(self) -> list[Asset]: ...
    def add_contract(self, contract: Contract) -> None: ...
    def get_contract(self, contract_id: str) -> Optional[Contract]: ...
    def get_all_contracts(self) -> list[Contract]: ...
    def get_assets_by_contract(self, contract_id: str) -> list[Asset]: ...
    def get_assets_by_definition(self, def_hash: str) -> list[Asset]: ...
    def get_latest_asset(self, def_hash: str) -> Optional[Asset]: ...
    def add_replacement_claim(self, claim) -> None: ...
    def get_claims_by_definition(self, definition_hash: str) -> list: ...
    def get_claims_for_asset(self, asset_id: str) -> list: ...
    def register_activation_refs(
        self, contract_id: str, asset_names: set[str]
    ) -> None: ...
    def unregister_activation_refs(self, contract_id: str) -> None: ...
    def register_declared_outputs(
        self, contract_id: str, output_names: set[str]
    ) -> None: ...
    def unregister_declared_outputs(self, contract_id: str) -> None: ...
    def get_contracts_waiting_for(self, asset_name: str) -> list[str]: ...
    def get_contracts_declaring_output(self, asset_name: str) -> list[str]: ...
    def rebuild_projection_indexes(self) -> None: ...
    def projection_index_digest(self) -> str: ...
    def append_runtime_record(self, record: RuntimeRecord) -> int: ...
    def get_runtime_record(self, record_id: str) -> RuntimeRecord | None: ...
    def scan_runtime_records(
        self, *, after_revision: int = 0, record_type: str | None = None
    ) -> list[tuple[int, RuntimeRecord]]: ...
    def get_runtime_revision(self) -> int: ...


@runtime_checkable
class OperationalStoreProtocol(StoreProtocol, Protocol):
    """Required transactional surface for claim/package/submit execution."""

    def get_worker_registration(self, worker_id: str): ...
    def get_worker_registrations(self) -> list: ...
    def get_by_contract(self, contract_id: str) -> list: ...
    def get_all(self) -> list: ...
    def new_entry(self, contract_id: str, event_type: str, **kwargs): ...
    def get_claim(self, contract_id: str) -> dict | None: ...
    def claim_contract(
        self,
        contract_id: str,
        worker_id: str,
        lease_seconds: int = 60,
        package_id: str = "",
        expected_registration_version: str = "",
    ) -> dict | None: ...
    def renew_claim(
        self,
        claim_id: str,
        epoch: int,
        worker_id: str,
        *,
        lease_seconds: int = 60,
    ) -> dict | None: ...
    def get_idempotency(
        self, contract_id: str, idempotency_key: str
    ) -> dict | None: ...
    def has_any_idempotency(self, contract_id: str) -> bool: ...
    def commit_candidate_submission(self, *args, **kwargs) -> bool: ...
    def commit_method_submission(self, *args, **kwargs) -> bool: ...


def require_operational_store(store: StoreProtocol) -> OperationalStoreProtocol:
    """Fail startup instead of silently downgrading transactional semantics."""
    if not isinstance(store, OperationalStoreProtocol):
        raise TypeError(
            f"{type(store).__name__} does not implement the required "
            "transactional worker StorePort"
        )
    return store


# ---------------------------------------------------------------------------
# Activation name extraction
# ---------------------------------------------------------------------------

_ACTIVATION_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT"})


def _extract_activation_names(expression: str) -> set[str]:
    """Extract asset names from an activation expression.

    Returns the set of non-keyword, non-punctuation tokens.
    For complex/unparseable expressions returns an empty set (pass-through).
    """
    import re

    if not expression or not expression.strip():
        return set()
    names: set[str] = set()
    for token in re.split(r"\s+", expression.strip()):
        token = token.strip("()")
        if not token:
            continue
        if token.upper() in _ACTIVATION_KEYWORDS:
            continue
        if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", token):
            names.add(token)
    return names


def _projection_index_digest(
    activation_rows: list[tuple[str, str]], output_rows: list[tuple[str, str]]
) -> str:
    payload = json.dumps(
        {
            "activation": sorted(activation_rows),
            "declared_outputs": sorted(output_rows),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return compute_content_hash(payload)


class _ProjectionIndexMixin:
    """Shared derived-index behavior for local record adapters."""

    contracts: dict[str, Contract]
    _activation_index: dict[str, set[str]]
    _reverse_activation_index: dict[str, set[str]]
    _declared_outputs_index: dict[str, set[str]]

    def register_activation_refs(self, contract_id: str, asset_names: set[str]) -> None:
        self.unregister_activation_refs(contract_id)
        if not asset_names:
            return
        self._activation_index[contract_id] = asset_names.copy()
        for name in asset_names:
            self._reverse_activation_index.setdefault(name, set()).add(contract_id)

    def unregister_activation_refs(self, contract_id: str) -> None:
        old = self._activation_index.pop(contract_id, None)
        if old:
            for name in old:
                targets = self._reverse_activation_index.get(name)
                if targets:
                    targets.discard(contract_id)
                    if not targets:
                        del self._reverse_activation_index[name]

    def register_declared_outputs(
        self, contract_id: str, output_names: set[str]
    ) -> None:
        self.unregister_declared_outputs(contract_id)
        for name in output_names:
            self._declared_outputs_index.setdefault(name, set()).add(contract_id)

    def unregister_declared_outputs(self, contract_id: str) -> None:
        empty_names: list[str] = []
        for name, contract_ids in self._declared_outputs_index.items():
            contract_ids.discard(contract_id)
            if not contract_ids:
                empty_names.append(name)
        for name in empty_names:
            del self._declared_outputs_index[name]

    def get_contracts_waiting_for(self, asset_name: str) -> list[str]:
        return list(self._reverse_activation_index.get(asset_name, set()))

    def get_contracts_declaring_output(self, asset_name: str) -> list[str]:
        return list(self._declared_outputs_index.get(asset_name, set()))

    def rebuild_projection_indexes(self) -> None:
        """Rebuild all derived indexes solely from immutable contracts."""
        self._activation_index.clear()
        self._reverse_activation_index.clear()
        self._declared_outputs_index.clear()
        for contract in self.contracts.values():
            self.register_activation_refs(
                contract.id, _extract_activation_names(contract.activation)
            )
            self.register_declared_outputs(contract.id, set(contract.outputs))

    def projection_index_digest(self) -> str:
        activation_rows = [
            (contract_id, asset_name)
            for contract_id, asset_names in self._activation_index.items()
            for asset_name in asset_names
        ]
        output_rows = [
            (contract_id, output_name)
            for output_name, contract_ids in self._declared_outputs_index.items()
            for contract_id in contract_ids
        ]
        return _projection_index_digest(activation_rows, output_rows)


class MemoryStore(_ProjectionIndexMixin):
    def __init__(self) -> None:
        self.assets: dict[str, Asset] = {}
        self.contracts: dict[str, Contract] = {}
        self._claims: list = []
        self._activation_index: dict[str, set[str]] = {}
        self._reverse_activation_index: dict[str, set[str]] = {}
        self._declared_outputs_index: dict[str, set[str]] = {}
        self._worker_registrations: dict[str, object] = {}
        self._runtime_records: dict[str, tuple[int, RuntimeRecord]] = {}
        self._runtime_revision = 0

    def register_worker(self, registration) -> None:
        record = worker_registration_record(registration)
        for _, existing in self.scan_runtime_records(record_type="worker.registered"):
            payload = existing.payload
            if (
                payload["worker_id"] == registration.worker_id
                and payload["version"] == registration.version
            ):
                if existing.id == record.id:
                    self._worker_registrations[registration.worker_id] = registration
                    return
                raise ImmutableRecordConflict(
                    "worker registration version",
                    f"{registration.worker_id}:{registration.version}",
                )
        self.append_runtime_record(record)
        self._worker_registrations[registration.worker_id] = registration

    def get_worker_registration(self, worker_id: str):
        return self._worker_registrations.get(worker_id)

    def get_worker_registrations(self) -> list:
        return list(self._worker_registrations.values())

    def rebuild_worker_registration_projection(self) -> None:
        self._worker_registrations.clear()
        for _, record in self.scan_runtime_records(record_type="worker.registered"):
            registration = registration_from_record(record)
            self._worker_registrations[registration.worker_id] = registration

    def append_runtime_record(self, record: RuntimeRecord) -> int:
        existing = self._runtime_records.get(record.id)
        if existing is not None:
            revision, existing_record = existing
            if runtime_record_effective_payload(
                existing_record
            ) == runtime_record_effective_payload(record):
                return revision
            raise ImmutableRecordConflict("runtime record", record.id)
        validate_runtime_record(record)
        self._runtime_revision += 1
        self._runtime_records[record.id] = (self._runtime_revision, record)
        return self._runtime_revision

    def get_runtime_record(self, record_id: str) -> RuntimeRecord | None:
        existing = self._runtime_records.get(record_id)
        return existing[1] if existing is not None else None

    def scan_runtime_records(
        self, *, after_revision: int = 0, record_type: str | None = None
    ) -> list[tuple[int, RuntimeRecord]]:
        return [
            (revision, record)
            for revision, record in self._runtime_records.values()
            if revision > after_revision
            and (record_type is None or record.record_type == record_type)
        ]

    def get_runtime_revision(self) -> int:
        return self._runtime_revision

    def add_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"G3/N-P1.6: Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        if _is_protected_name(asset.name):
            prefix = next(
                (
                    p
                    for p in RESERVED_PREFIXES
                    if asset.name.startswith(p)
                    or (p.endswith("_") and asset.name == p.rstrip("_"))
                ),
                "?",
            )
            raise ReservedNamespaceError(asset.name, prefix)
        existing = self.assets.get(asset.id)
        if existing is not None:
            if existing == asset:
                return
            raise ImmutableRecordConflict("asset", asset.id)
        self.assets[asset.id] = asset

    def _add_system_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"G3/N-P1.6: Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        self._persist_asset(asset)

    def _persist_asset(self, asset: Asset) -> None:
        existing = self.assets.get(asset.id)
        if existing is not None:
            if existing == asset:
                return
            raise ImmutableRecordConflict("asset", asset.id)
        self.assets[asset.id] = asset

    def get_asset(self, asset_id: str) -> Optional[Asset]:
        return self.assets.get(asset_id)

    def get_assets_by_name(self, name: str) -> list[Asset]:
        return [a for a in self.assets.values() if a.name == name]

    def has_asset_named(self, name: str) -> bool:
        return any(a.name == name for a in self.assets.values())

    def get_all_assets(self) -> list[Asset]:
        return list(self.assets.values())

    def add_contract(self, contract: Contract) -> None:
        existing = self.contracts.get(contract.id)
        if existing is not None:
            if existing == contract:
                return
            raise ImmutableRecordConflict("contract", contract.id)
        self.contracts[contract.id] = contract
        self.register_activation_refs(
            contract.id, _extract_activation_names(contract.activation)
        )
        self.register_declared_outputs(contract.id, set(contract.outputs))

    def get_contract(self, contract_id: str) -> Optional[Contract]:
        return self.contracts.get(contract_id)

    def get_all_contracts(self) -> list[Contract]:
        return list(self.contracts.values())

    def get_assets_by_contract(self, contract_id: str) -> list[Asset]:
        return [a for a in self.assets.values() if a.created_by == contract_id]

    def get_assets_by_definition(self, def_hash: str) -> list[Asset]:
        return [a for a in self.assets.values() if a.definition_hash == def_hash]

    def get_latest_asset(self, def_hash: str) -> Optional[Asset]:
        latest: Optional[Asset] = None
        for asset in self.assets.values():
            if asset.definition_hash == def_hash:
                latest = asset
        return latest

    def add_replacement_claim(self, claim) -> None:
        for existing in self._claims:
            if existing.id != claim.id:
                continue
            if existing == claim:
                return
            raise ImmutableRecordConflict("replacement claim", claim.id)
        self._claims.append(claim)

    def get_claims_by_definition(self, definition_hash: str) -> list:
        return [c for c in self._claims if c.definition_hash == definition_hash]

    def get_claims_for_asset(self, asset_id: str) -> list:
        return [c for c in self._claims if c.source_asset_id == asset_id]


class JsonLStore(_ProjectionIndexMixin):
    """Persistent JSONL store for Assets and Contracts — one JSON object per line."""

    def __init__(self, assets_path: str, contracts_path: str) -> None:
        self._assets_path = assets_path
        self._contracts_path = contracts_path

        for p in (assets_path, contracts_path):
            parent = Path(p).parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)

        self.assets: dict[str, Asset] = {}
        self.contracts: dict[str, Contract] = {}
        self._name_index: dict[str, list[str]] = {}
        self._created_by_index: dict[str, list[str]] = {}
        self._activation_index: dict[str, set[str]] = {}
        self._reverse_activation_index: dict[str, set[str]] = {}
        self._declared_outputs_index: dict[str, set[str]] = {}

        self._load_assets()
        self._load_contracts()
        self.rebuild_projection_indexes()

    def _load_assets(self) -> None:
        if not os.path.exists(self._assets_path):
            return
        with open(self._assets_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                data = json.loads(stripped)
                asset = Asset(
                    id=data["id"],
                    name=data["name"],
                    content=data["content"],
                    content_type=data.get("content_type", "text"),
                    created_by=data.get("created_by", ""),
                    origin=data.get("origin", ""),
                    trust_tier=data.get("trust_tier", "untrusted"),
                    minted_by=data.get("minted_by", ""),
                    source_uri=data.get("source_uri", ""),
                    signed_by=data.get("signed_by", ""),
                    signer_kind=data.get("signer_kind", "deterministic"),
                    provenance_seal=data.get(
                        "provenance_seal", data.get("signature", "")
                    ),
                    definition_hash=data.get("definition_hash", ""),
                    content_hash=data.get("content_hash", ""),
                    promptable=data.get("promptable", True),
                    disclosure_view=data.get("disclosure_view", "original"),
                    keep_flag=data.get("keep_flag", False),
                    tombstoned=data.get("tombstoned", False),
                    tombstoned_at=data.get("tombstoned_at"),
                    lineage_id=data.get("lineage_id", ""),
                )
                existing = self.assets.get(asset.id)
                if existing is not None:
                    if existing == asset:
                        continue
                    raise ImmutableRecordConflict("asset", asset.id)
                self.assets[asset.id] = asset
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self._name_index.clear()
        self._created_by_index.clear()
        for asset in self.assets.values():
            self._name_index.setdefault(asset.name, []).append(asset.id)
            if asset.created_by:
                self._created_by_index.setdefault(asset.created_by, []).append(asset.id)

    def _load_contracts(self) -> None:
        if not os.path.exists(self._contracts_path):
            return
        with open(self._contracts_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                data = json.loads(stripped)
                contract = Contract(
                    id=data["id"],
                    parent_id=data.get("parent_id"),
                    name=data.get("name", ""),
                    description=data.get("description", ""),
                    inputs=data.get("inputs", []),
                    outputs=data.get("outputs", []),
                    activation=data.get("activation", ""),
                    budget=data.get("budget", 0),
                    tool_scope=data.get("tool_scope", []),
                    labels=data.get("labels", []),
                    worker_capabilities=data.get("worker_capabilities", []),
                    worker_pools=data.get("worker_pools", []),
                    origin=data.get("origin", "human"),
                    minting_authority=data.get("minting_authority", []),
                    sensitive_input_policy=data.get("sensitive_input_policy"),
                )
                existing = self.contracts.get(contract.id)
                if existing is not None:
                    if existing == contract:
                        continue
                    raise ImmutableRecordConflict("contract", contract.id)
                self.contracts[contract.id] = contract

    @staticmethod
    def _write_jsonl_line(path: str, line: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                _logger.warning("fsync failed for %s", path)

    def add_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"G3/N-P1.6: Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        if _is_protected_name(asset.name):
            prefix = next(
                (
                    p
                    for p in RESERVED_PREFIXES
                    if asset.name.startswith(p)
                    or (p.endswith("_") and asset.name == p.rstrip("_"))
                ),
                "?",
            )
            raise ReservedNamespaceError(asset.name, prefix)
        self._persist_asset(asset)

    def _add_system_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"G3/N-P1.6: Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        self._persist_asset(asset)

    def _persist_asset(self, asset: Asset) -> None:
        existing = self.assets.get(asset.id)
        if existing is not None:
            if existing == asset:
                return
            raise ImmutableRecordConflict("asset", asset.id)
        line = json.dumps(asset_to_dict(asset), ensure_ascii=False) + "\n"
        self._write_jsonl_line(self._assets_path, line)
        self.assets[asset.id] = asset
        self._name_index.setdefault(asset.name, []).append(asset.id)
        if asset.created_by:
            self._created_by_index.setdefault(asset.created_by, []).append(asset.id)

    def get_asset(self, asset_id: str) -> Optional[Asset]:
        return self.assets.get(asset_id)

    def get_assets_by_name(self, name: str) -> list[Asset]:
        ids = self._name_index.get(name, [])
        return [self.assets[aid] for aid in ids if aid in self.assets]

    def has_asset_named(self, name: str) -> bool:
        return name in self._name_index and len(self._name_index[name]) > 0

    def get_all_assets(self) -> list[Asset]:
        return list(self.assets.values())

    def get_assets_by_contract(self, contract_id: str) -> list[Asset]:
        ids = self._created_by_index.get(contract_id, [])
        return [self.assets[aid] for aid in ids if aid in self.assets]

    def get_assets_by_definition(self, def_hash: str) -> list[Asset]:
        return [a for a in self.assets.values() if a.definition_hash == def_hash]

    def get_latest_asset(self, def_hash: str) -> Optional[Asset]:
        latest: Optional[Asset] = None
        for asset in self.assets.values():
            if asset.definition_hash == def_hash:
                latest = asset
        return latest

    def add_contract(self, contract: Contract) -> None:
        existing = self.contracts.get(contract.id)
        if existing is not None:
            if existing == contract:
                return
            raise ImmutableRecordConflict("contract", contract.id)
        line = json.dumps(contract_to_dict(contract), ensure_ascii=False) + "\n"
        self._write_jsonl_line(self._contracts_path, line)
        self.contracts[contract.id] = contract
        self.register_activation_refs(
            contract.id, _extract_activation_names(contract.activation)
        )
        self.register_declared_outputs(contract.id, set(contract.outputs))

    def get_contract(self, contract_id: str) -> Optional[Contract]:
        return self.contracts.get(contract_id)

    def get_all_contracts(self) -> list[Contract]:
        return list(self.contracts.values())
