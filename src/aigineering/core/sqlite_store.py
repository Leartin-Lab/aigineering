"""SQLite transactional store for Assets, Contracts, and related entities.

Implements StoreProtocol with WAL-mode SQLite for concurrent reads,
schema versioning with migration hooks, and index coverage for all
lookup fields.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Optional

from aigineering.core.authority import (
    ReservedNamespaceError,
    _is_protected_name,
    matched_reserved_prefix,
)
from aigineering.core.activation import activation_names
from aigineering.core.crash import check_crash_point
from aigineering.core.ids import (
    compute_content_hash,
    now_iso,
    validate_contract_identity,
)
from aigineering.core.actor_facts import validate_actor_runtime_record
from aigineering.core.asset_graph_facts import validate_asset_graph_record
from aigineering.core.lifecycle_facts import validate_terminal_record
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.asset_versions import (
    replacement_claim_from_record,
    replacement_claim_payload,
)
from aigineering.core.store import _projection_index_digest
from aigineering.core.sqlite_migrations import (
    CURRENT_SCHEMA_VERSION as CURRENT_SCHEMA_VERSION,
    current_schema_version,
    initialize_sqlite_schema,
)
from aigineering.core.trace import entry_references_asset, trace_effective_payload
from aigineering.core.worker_routing import (
    registration_is_replay,
    WorkerRegistration,
    registration_from_record,
    worker_registration_record,
)
from aigineering.protocol.types import Asset, Contract, ReplacementClaim, TraceEntry
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    create_runtime_record,
    runtime_record_effective_payload,
    validate_runtime_record,
)
from aigineering.protocol.wire import (
    asset_to_dict,
    contract_to_dict,
    trace_entry_from_dict,
    trace_entry_to_dict,
)

_logger = logging.getLogger(__name__)


class WorkerBindingConflict(ValueError):
    """The registered worker actor/key changed before Candidate commitment."""


def _is_sqlite_contention(exc: sqlite3.OperationalError) -> bool:
    """Return whether an OperationalError is expected writer contention."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    message = str(exc).lower()
    return "locked" in message or "busy" in message


# ---------------------------------------------------------------------------
# SQLiteStore
# ---------------------------------------------------------------------------


class SQLiteStore:
    """SQLite-backed store implementing StoreProtocol.

    Uses WAL mode for concurrent reads and explicit transactions for all
    writes.  Supports both in-memory (``:memory:``) and file-based
    databases.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path

        if db_path != ":memory:":
            parent = Path(db_path).parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")

        # WAL only applies to file-backed databases; :memory: uses "memory" journal
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")

        initialize_sqlite_schema(self._conn, self)

    # ── Immutable runtime-record envelope ────────────────────────────────

    def append_runtime_record(self, record: RuntimeRecord) -> int:
        with self._conn:
            return self._insert_runtime_record(record)

    def _insert_runtime_record(self, record: RuntimeRecord) -> int:
        """Insert within the caller's transaction without committing it."""
        row = self._conn.execute(
            "SELECT * FROM runtime_records WHERE record_id = ?", (record.id,)
        ).fetchone()
        if row is not None:
            existing = self._row_to_runtime_record(row)
            if runtime_record_effective_payload(
                existing
            ) != runtime_record_effective_payload(record):
                raise ImmutableRecordConflict("runtime record", record.id)
            registration = (
                registration_from_record(existing)
                if existing.record_type == "worker.registered"
                else None
            )
            if registration is not None:
                self._upsert_worker_registration(registration)
            replacement_claim = replacement_claim_from_record(existing)
            if replacement_claim is not None:
                self._insert_replacement_claim(replacement_claim)
            return int(row["revision"])
        validate_actor_runtime_record(record, self)
        validate_asset_graph_record(record, self)
        if record.record_type == "lifecycle.terminal":
            validate_terminal_record(
                record,
                self.scan_runtime_records(record_type="lifecycle.terminal"),
            )
            contract_id = str(record.payload["contract_id"])
            if self.get_contract(contract_id) is None:
                raise ValueError(
                    f"lifecycle.terminal references unknown Contract {contract_id!r}"
                )
        registration = None
        if record.record_type == "worker.registered":
            registration = registration_from_record(record)
            registration_is_replay(
                self.scan_runtime_records(record_type="worker.registered"),
                registration,
            )
        replacement_claim = replacement_claim_from_record(record)
        validate_runtime_record(record)
        try:
            cursor = self._conn.execute(
                """INSERT INTO runtime_records (
                    record_id, record_type, schema_version, payload_json,
                    causal_parents, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.record_type,
                    record.schema_version,
                    json.dumps(
                        runtime_record_effective_payload(record)["payload"],
                        sort_keys=True,
                    ),
                    json.dumps(list(record.causal_parents)),
                    record.recorded_at,
                ),
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                "SELECT * FROM runtime_records WHERE record_id = ?", (record.id,)
            ).fetchone()
            if row is not None and runtime_record_effective_payload(
                self._row_to_runtime_record(row)
            ) == runtime_record_effective_payload(record):
                return int(row["revision"])
            raise ImmutableRecordConflict("runtime record", record.id) from None
        if registration is not None:
            self._upsert_worker_registration(registration)
        if replacement_claim is not None:
            self._insert_replacement_claim(replacement_claim)
        return int(cursor.lastrowid)

    def get_runtime_record(self, record_id: str) -> RuntimeRecord | None:
        row = self._conn.execute(
            "SELECT * FROM runtime_records WHERE record_id = ?", (record_id,)
        ).fetchone()
        return self._row_to_runtime_record(row) if row is not None else None

    def scan_runtime_records(
        self, *, after_revision: int = 0, record_type: str | None = None
    ) -> list[tuple[int, RuntimeRecord]]:
        if record_type is None:
            rows = self._conn.execute(
                "SELECT * FROM runtime_records WHERE revision > ? ORDER BY revision",
                (after_revision,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runtime_records "
                "WHERE revision > ? AND record_type = ? ORDER BY revision",
                (after_revision, record_type),
            ).fetchall()
        return [
            (int(row["revision"]), self._row_to_runtime_record(row)) for row in rows
        ]

    def get_runtime_revision(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM runtime_records"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _row_to_runtime_record(row: sqlite3.Row) -> RuntimeRecord:
        return RuntimeRecord(
            id=row["record_id"],
            record_type=row["record_type"],
            schema_version=int(row["schema_version"]),
            payload=json.loads(row["payload_json"]),
            causal_parents=tuple(json.loads(row["causal_parents"])),
            recorded_at=row["recorded_at"],
        )

    # ── Runtime Lifecycle ─────────────────────────────────────────────────

    def upsert_runtime_lifecycle(
        self, runtime_id: str, heartbeat_at: str, state: str
    ) -> None:
        """Insert or update a runtime lifecycle record."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO runtime_lifecycle (runtime_id, heartbeat_at, state, started_at) "
                "VALUES (?, ?, ?, COALESCE((SELECT started_at FROM runtime_lifecycle "
                "WHERE runtime_id = ?), ?)) "
                "ON CONFLICT(runtime_id) DO UPDATE SET "
                "heartbeat_at = excluded.heartbeat_at, state = excluded.state"
                + (", stopped_at = ?" if state == "stopped" else ""),
                (runtime_id, heartbeat_at, state, runtime_id, heartbeat_at)
                + ((heartbeat_at,) if state == "stopped" else ()),
            )

    def get_runtime_lifecycle(self, runtime_id: str) -> dict | None:
        """Return the lifecycle record for *runtime_id*, or None."""
        row = self._conn.execute(
            "SELECT runtime_id, heartbeat_at, state, started_at, stopped_at "
            "FROM runtime_lifecycle WHERE runtime_id = ?",
            (runtime_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_orphaned_runtimes(self, ttl_seconds: int) -> list[dict]:
        """Return lifecycle records for active runtimes whose heartbeat
        is older than *ttl_seconds* seconds from now."""
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        ).isoformat()
        rows = self._conn.execute(
            "SELECT runtime_id, heartbeat_at, state, started_at, stopped_at "
            "FROM runtime_lifecycle "
            "WHERE state = 'active' AND heartbeat_at < ?",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    @property
    def schema_version(self) -> int:
        return current_schema_version(self._conn)

    # ------------------------------------------------------------------
    # Row → dataclass helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_asset(row: sqlite3.Row) -> Asset:
        return Asset(
            id=row["id"],
            name=row["name"],
            content=row["content"],
            content_type=row["content_type"],
            created_by=row["created_by"],
            origin=row["origin"],
            trust_tier=row["trust_tier"],
            minted_by=row["minted_by"],
            source_uri=row["source_uri"],
            signed_by=row["signed_by"],
            signer_kind=row["signer_kind"],
            provenance_seal=row["provenance_seal"],
            promptable=bool(row["promptable"]),
            disclosure_view=row["disclosure_view"],
            definition_hash=row["definition_hash"],
            content_hash=row["content_hash"],
            keep_flag=bool(row["keep_flag"]),
            tombstoned=bool(row["tombstoned"]),
            tombstoned_at=row["tombstoned_at"],
            lineage_id=row["lineage_id"],
        )

    @staticmethod
    def _row_to_contract(row: sqlite3.Row) -> Contract:
        return Contract(
            id=row["id"],
            parent_id=row["parent_id"],
            name=row["name"],
            description=row["description"],
            inputs=tuple(json.loads(row["inputs"])),
            outputs=tuple(json.loads(row["outputs"])),
            activation=row["activation"],
            budget=row["budget"],
            tool_scope=tuple(json.loads(row["tool_scope"])),
            labels=tuple(json.loads(row["labels"])),
            worker_capabilities=tuple(json.loads(row["worker_capabilities"] or "[]")),
            worker_pools=tuple(json.loads(row["worker_pools"] or "[]")),
            origin=row["origin"],
            minting_authority=tuple(json.loads(row["minting_authority"] or "[]")),
            sensitive_input_policy=(
                json.loads(row["sensitive_input_policy"])
                if row["sensitive_input_policy"]
                else None
            ),
            acceptance_policy=(
                json.loads(row["acceptance_policy"])
                if row["acceptance_policy"]
                else None
            ),
        )

    # ------------------------------------------------------------------
    # StoreProtocol: assets
    # ------------------------------------------------------------------

    def add_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        if _is_protected_name(asset.name):
            prefix = matched_reserved_prefix(asset.name) or "?"
            raise ReservedNamespaceError(asset.name, prefix)
        with self._conn:
            self._insert_asset(asset)

    def _add_system_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        with self._conn:
            self._insert_asset(asset)

    def _insert_asset(self, asset: Asset) -> None:
        existing = self.get_asset(asset.id)
        if existing is not None:
            if existing == asset:
                return
            raise ImmutableRecordConflict("asset", asset.id)
        d = asset_to_dict(asset)
        try:
            self._conn.execute(
                """INSERT INTO assets (
                    id, name, content, content_type, created_by,
                    origin, trust_tier, minted_by, source_uri,
                    signed_by, signer_kind, provenance_seal, promptable, disclosure_view,
                    definition_hash, content_hash,
                    keep_flag, tombstoned, tombstoned_at, lineage_id
                ) VALUES (
                    :id, :name, :content, :content_type, :created_by,
                    :origin, :trust_tier, :minted_by, :source_uri,
                    :signed_by, :signer_kind, :provenance_seal, :promptable, :disclosure_view,
                    :definition_hash, :content_hash,
                    :keep_flag, :tombstoned, :tombstoned_at, :lineage_id
                )""",
                {
                    "id": d["id"],
                    "name": d["name"],
                    "content": d["content"],
                    "content_type": d["content_type"],
                    "created_by": d["created_by"],
                    "origin": d["origin"],
                    "trust_tier": d["trust_tier"],
                    "minted_by": d["minted_by"],
                    "source_uri": d["source_uri"],
                    "signed_by": d["signed_by"],
                    "signer_kind": d["signer_kind"],
                    "provenance_seal": d["provenance_seal"],
                    "promptable": int(d["promptable"]),
                    "disclosure_view": d["disclosure_view"],
                    "definition_hash": d["definition_hash"],
                    "content_hash": d["content_hash"],
                    "keep_flag": int(d["keep_flag"]),
                    "tombstoned": int(d["tombstoned"]),
                    "tombstoned_at": d["tombstoned_at"],
                    "lineage_id": d["lineage_id"],
                },
            )
        except sqlite3.IntegrityError:
            existing = self.get_asset(asset.id)
            if existing == asset:
                return
            raise ImmutableRecordConflict("asset", asset.id) from None

    def get_asset(self, asset_id: str) -> Optional[Asset]:
        cur = self._conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,))
        row = cur.fetchone()
        return self._row_to_asset(row) if row else None

    def get_assets_by_name(self, name: str) -> list[Asset]:
        cur = self._conn.execute("SELECT * FROM assets WHERE name = ?", (name,))
        return [self._row_to_asset(row) for row in cur.fetchall()]

    def has_asset_named(self, name: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM assets WHERE name = ? LIMIT 1", (name,))
        return cur.fetchone() is not None

    def get_all_assets(self) -> list[Asset]:
        cur = self._conn.execute("SELECT * FROM assets")
        return [self._row_to_asset(row) for row in cur.fetchall()]

    def get_assets_by_contract(self, contract_id: str) -> list[Asset]:
        cur = self._conn.execute(
            "SELECT * FROM assets WHERE created_by = ?", (contract_id,)
        )
        return [self._row_to_asset(row) for row in cur.fetchall()]

    def get_assets_by_definition(self, def_hash: str) -> list[Asset]:
        cur = self._conn.execute(
            "SELECT * FROM assets WHERE definition_hash = ?", (def_hash,)
        )
        return [self._row_to_asset(row) for row in cur.fetchall()]

    def get_latest_asset(self, def_hash: str) -> Optional[Asset]:
        cur = self._conn.execute(
            "SELECT * FROM assets WHERE definition_hash = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (def_hash,),
        )
        row = cur.fetchone()
        return self._row_to_asset(row) if row else None

    # ------------------------------------------------------------------
    # StoreProtocol: contracts
    # ------------------------------------------------------------------

    def _insert_contract(self, contract: Contract) -> None:
        """Insert an immutable contract row + derived index rows."""
        validate_contract_identity(contract)
        existing = self.get_contract(contract.id)
        if existing is not None:
            if existing == contract:
                return
            raise ImmutableRecordConflict("contract", contract.id)
        d = contract_to_dict(contract)
        try:
            self._conn.execute(
                """INSERT INTO contracts (
                id, parent_id, name, description,
                inputs, outputs, activation, budget,
                tool_scope, labels, worker_capabilities, worker_pools,
                origin, minting_authority, sensitive_input_policy, acceptance_policy
            ) VALUES (
                :id, :parent_id, :name, :description,
                :inputs, :outputs, :activation, :budget,
                :tool_scope, :labels, :worker_capabilities, :worker_pools,
                :origin, :minting_authority, :sensitive_input_policy, :acceptance_policy
            )""",
                {
                    "id": d["id"],
                    "parent_id": d["parent_id"],
                    "name": d["name"],
                    "description": d["description"],
                    "inputs": json.dumps(list(d["inputs"])),
                    "outputs": json.dumps(list(d["outputs"])),
                    "activation": d["activation"],
                    "budget": d["budget"],
                    "tool_scope": json.dumps(list(d["tool_scope"])),
                    "labels": json.dumps(list(d["labels"])),
                    "worker_capabilities": json.dumps(list(d["worker_capabilities"])),
                    "worker_pools": json.dumps(list(d["worker_pools"])),
                    "origin": d["origin"],
                    "minting_authority": json.dumps(list(d["minting_authority"])),
                    "sensitive_input_policy": (
                        json.dumps(d["sensitive_input_policy"], sort_keys=True)
                        if d["sensitive_input_policy"] is not None
                        else None
                    ),
                    "acceptance_policy": (
                        json.dumps(d["acceptance_policy"], sort_keys=True)
                        if d["acceptance_policy"] is not None
                        else None
                    ),
                },
            )
        except sqlite3.IntegrityError:
            existing = self.get_contract(contract.id)
            if existing == contract:
                return
            raise ImmutableRecordConflict("contract", contract.id) from None
        self._conn.execute(
            "DELETE FROM contract_activation_refs WHERE contract_id = ?",
            (contract.id,),
        )
        for name in activation_names(contract.activation):
            self._conn.execute(
                "INSERT INTO contract_activation_refs (contract_id, asset_name) "
                "VALUES (?, ?)",
                (contract.id, name),
            )
        self._conn.execute(
            "DELETE FROM contract_declared_outputs WHERE contract_id = ?",
            (contract.id,),
        )
        for name in contract.outputs:
            self._conn.execute(
                "INSERT INTO contract_declared_outputs (contract_id, output_name) "
                "VALUES (?, ?)",
                (contract.id, name),
            )

    def add_contract(self, contract: Contract) -> None:
        with self._conn:
            self._insert_contract(contract)

    def get_contract(self, contract_id: str) -> Optional[Contract]:
        cur = self._conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,))
        row = cur.fetchone()
        return self._row_to_contract(row) if row else None

    def get_all_contracts(self) -> list[Contract]:
        cur = self._conn.execute("SELECT * FROM contracts")
        return [self._row_to_contract(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Worker registration / routing control plane
    # ------------------------------------------------------------------

    def register_worker(self, registration: WorkerRegistration) -> None:
        """Append one registration version and update its derived current view."""
        self.append_runtime_record(worker_registration_record(registration))

    def _upsert_worker_registration(self, registration: WorkerRegistration) -> None:
        self._conn.execute(
            """INSERT INTO worker_registrations (
                worker_id, actor_id, key_id, capabilities, pools, profile_id,
                capacity, enabled, version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                actor_id = excluded.actor_id,
                key_id = excluded.key_id,
                capabilities = excluded.capabilities,
                pools = excluded.pools,
                profile_id = excluded.profile_id,
                capacity = excluded.capacity,
                enabled = excluded.enabled,
                version = excluded.version,
                updated_at = excluded.updated_at""",
            (
                registration.worker_id,
                registration.actor_id,
                registration.key_id,
                json.dumps(list(registration.capabilities)),
                json.dumps(list(registration.pools)),
                registration.profile_id,
                registration.capacity,
                int(registration.enabled),
                registration.version,
                now_iso(),
            ),
        )

    def rebuild_worker_registration_projection(self) -> None:
        """Rebuild the mutable routing view from immutable registration facts."""
        with self._conn:
            self._conn.execute("DELETE FROM worker_registrations")
            for _, record in self.scan_runtime_records(record_type="worker.registered"):
                registration = registration_from_record(record)
                self._conn.execute(
                    """INSERT INTO worker_registrations (
                        worker_id, actor_id, key_id, capabilities, pools,
                        profile_id, capacity, enabled, version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(worker_id) DO UPDATE SET
                        actor_id = excluded.actor_id,
                        key_id = excluded.key_id,
                        capabilities = excluded.capabilities,
                        pools = excluded.pools,
                        profile_id = excluded.profile_id,
                        capacity = excluded.capacity,
                        enabled = excluded.enabled,
                        version = excluded.version,
                        updated_at = excluded.updated_at""",
                    (
                        registration.worker_id,
                        registration.actor_id,
                        registration.key_id,
                        json.dumps(list(registration.capabilities)),
                        json.dumps(list(registration.pools)),
                        registration.profile_id,
                        registration.capacity,
                        int(registration.enabled),
                        registration.version,
                        record.recorded_at,
                    ),
                )

    def get_worker_registration(self, worker_id: str) -> WorkerRegistration | None:
        row = self._conn.execute(
            "SELECT * FROM worker_registrations WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        return self._row_to_worker_registration(row) if row is not None else None

    def get_worker_registrations(self) -> list[WorkerRegistration]:
        rows = self._conn.execute(
            "SELECT * FROM worker_registrations ORDER BY worker_id"
        ).fetchall()
        return [self._row_to_worker_registration(row) for row in rows]

    def _row_to_worker_registration(self, row: sqlite3.Row) -> WorkerRegistration:
        active_claims = self._conn.execute(
            "SELECT COUNT(*) FROM worker_claims WHERE worker_id = ? AND status = 'active'",
            (row["worker_id"],),
        ).fetchone()[0]
        return WorkerRegistration(
            worker_id=row["worker_id"],
            actor_id=row["actor_id"],
            key_id=row["key_id"],
            capabilities=tuple(json.loads(row["capabilities"])),
            pools=tuple(json.loads(row["pools"])),
            profile_id=row["profile_id"],
            capacity=row["capacity"],
            active_claims=active_claims,
            enabled=bool(row["enabled"]),
            version=row["version"],
        )

    # ------------------------------------------------------------------
    # Activation / declared-output indexes
    # ------------------------------------------------------------------

    def register_activation_refs(self, contract_id: str, asset_names: set[str]) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM contract_activation_refs WHERE contract_id = ?",
                (contract_id,),
            )
            for name in asset_names:
                self._conn.execute(
                    "INSERT INTO contract_activation_refs (contract_id, asset_name) "
                    "VALUES (?, ?)",
                    (contract_id, name),
                )

    def unregister_activation_refs(self, contract_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM contract_activation_refs WHERE contract_id = ?",
                (contract_id,),
            )

    def register_declared_outputs(
        self, contract_id: str, output_names: set[str]
    ) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM contract_declared_outputs WHERE contract_id = ?",
                (contract_id,),
            )
            for name in output_names:
                self._conn.execute(
                    "INSERT INTO contract_declared_outputs (contract_id, output_name) "
                    "VALUES (?, ?)",
                    (contract_id, name),
                )

    def unregister_declared_outputs(self, contract_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM contract_declared_outputs WHERE contract_id = ?",
                (contract_id,),
            )

    def get_contracts_waiting_for(self, asset_name: str) -> list[str]:
        cur = self._conn.execute(
            "SELECT contract_id FROM contract_activation_refs WHERE asset_name = ?",
            (asset_name,),
        )
        return [row[0] for row in cur.fetchall()]

    def get_contracts_declaring_output(self, asset_name: str) -> list[str]:
        cur = self._conn.execute(
            "SELECT contract_id FROM contract_declared_outputs WHERE output_name = ?",
            (asset_name,),
        )
        return [row[0] for row in cur.fetchall()]

    def rebuild_projection_indexes(self) -> None:
        """Atomically rebuild derived indexes from immutable contracts."""
        with self._conn:
            self._conn.execute("DELETE FROM contract_activation_refs")
            self._conn.execute("DELETE FROM contract_declared_outputs")
            for contract in self.get_all_contracts():
                for name in activation_names(contract.activation):
                    self._conn.execute(
                        "INSERT INTO contract_activation_refs "
                        "(contract_id, asset_name) VALUES (?, ?)",
                        (contract.id, name),
                    )
                for name in contract.outputs:
                    self._conn.execute(
                        "INSERT INTO contract_declared_outputs "
                        "(contract_id, output_name) VALUES (?, ?)",
                        (contract.id, name),
                    )

    def projection_index_digest(self) -> str:
        activation_rows = [
            (row[0], row[1])
            for row in self._conn.execute(
                "SELECT contract_id, asset_name FROM contract_activation_refs"
            ).fetchall()
        ]
        output_rows = [
            (row[0], row[1])
            for row in self._conn.execute(
                "SELECT contract_id, output_name FROM contract_declared_outputs"
            ).fetchall()
        ]
        return _projection_index_digest(activation_rows, output_rows)

    def runtime_materialization_digest(self) -> str:
        """Hash semantic materialized views, excluding cache timestamps."""
        registrations = [
            {
                "worker_id": registration.worker_id,
                "capabilities": registration.capabilities,
                "pools": registration.pools,
                "profile_id": registration.profile_id,
                "capacity": registration.capacity,
                "enabled": registration.enabled,
                "version": registration.version,
            }
            for registration in self.get_worker_registrations()
        ]
        claim_rows = self._conn.execute(
            "SELECT claim_id, contract_id, worker_id, lease_until, status, "
            "package_id, epoch FROM worker_claims ORDER BY contract_id, epoch"
        ).fetchall()
        idempotency_rows = self._conn.execute(
            "SELECT contract_id, idempotency_key, result_json "
            "FROM idempotency_records ORDER BY contract_id, idempotency_key"
        ).fetchall()
        replacement_claims = [
            replacement_claim_payload(self._row_to_replacement_claim(row))
            for row in self._conn.execute("SELECT * FROM claims ORDER BY id").fetchall()
        ]
        payload = {
            "assets": sorted(
                (asset_to_dict(asset) for asset in self.get_all_assets()),
                key=lambda row: row["id"],
            ),
            "claims": [dict(row) for row in claim_rows],
            "contracts": sorted(
                (contract_to_dict(contract) for contract in self.get_all_contracts()),
                key=lambda row: row["id"],
            ),
            "idempotency": [
                {
                    "contract_id": row["contract_id"],
                    "idempotency_key": row["idempotency_key"],
                    "result": json.loads(row["result_json"]),
                }
                for row in idempotency_rows
            ],
            "projection_indexes": self.projection_index_digest(),
            "registrations": registrations,
            "replacement_claims": replacement_claims,
            "trace": [
                trace_entry_to_dict(entry)
                for entry in sorted(self.get_all(), key=lambda item: item.id)
            ],
        }
        return compute_content_hash(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )

    def rebuild_runtime_materializations(self, *, strict: bool = True) -> str:
        """Rebuild Contract/Asset/Trace and control heads from RuntimeRecords."""
        records = self.scan_runtime_records()
        contract_payloads = {
            record.payload["contract"]["id"]: dict(record.payload["contract"])
            for _, record in records
            if record.record_type == "contract.declared"
        }
        asset_payloads = {
            record.payload["asset"]["id"]: dict(record.payload["asset"])
            for _, record in records
            if record.record_type == "asset.committed"
        }
        trace_payloads = {
            record.payload["trace"]["id"]: dict(record.payload["trace"])
            for _, record in records
            if record.record_type == "trace.recorded"
        }
        idempotency_payloads = {
            (str(record.payload["contract_id"]), str(record.payload["key"])): (
                runtime_record_effective_payload(record)["payload"]["result"]
            )
            for _, record in records
            if record.record_type == "idempotency.bound"
        }
        replacement_payloads = {
            str(record.payload["claim"]["id"]): runtime_record_effective_payload(
                record
            )["payload"]["claim"]
            for _, record in records
            if record.record_type == "replacement.claimed"
        }
        if strict:
            current_idempotency = {
                (row["contract_id"], row["idempotency_key"])
                for row in self._conn.execute(
                    "SELECT contract_id, idempotency_key FROM idempotency_records"
                ).fetchall()
            }
            missing = {
                "assets": sorted(
                    {asset.id for asset in self.get_all_assets()}
                    - asset_payloads.keys()
                ),
                "contracts": sorted(
                    {contract.id for contract in self.get_all_contracts()}
                    - contract_payloads.keys()
                ),
                "idempotency": sorted(
                    current_idempotency - idempotency_payloads.keys()
                ),
                "replacement_claims": sorted(
                    {
                        row["id"]
                        for row in self._conn.execute(
                            "SELECT id FROM claims"
                        ).fetchall()
                    }
                    - replacement_payloads.keys()
                ),
                "trace": sorted(
                    {entry.id for entry in self.get_all()} - trace_payloads.keys()
                ),
            }
            if any(missing.values()):
                raise RuntimeError(
                    "runtime materialization contains facts absent from immutable log: "
                    + json.dumps(missing, sort_keys=True)
                )

        with self._conn:
            self._conn.execute("DELETE FROM contract_activation_refs")
            self._conn.execute("DELETE FROM contract_declared_outputs")
            self._conn.execute("DELETE FROM trace_events")
            self._conn.execute("DELETE FROM idempotency_records")
            self._conn.execute("DELETE FROM claims")
            self._conn.execute("DELETE FROM assets")
            self._conn.execute("DELETE FROM contracts")
            for payload in contract_payloads.values():
                self._insert_contract(Contract(**payload))
            for payload in asset_payloads.values():
                asset = Asset(**payload)
                if not asset.signed_by or not verify_asset_seal(asset):
                    raise ValueError(
                        f"runtime fact for Asset '{asset.id}' has an invalid seal"
                    )
                self._insert_asset(asset)
            for payload in trace_payloads.values():
                self._insert_trace_entry(trace_entry_from_dict(payload))
            for (contract_id, key), result in idempotency_payloads.items():
                self.set_idempotency(contract_id, key, result)
            for payload in replacement_payloads.values():
                self._insert_replacement_claim(ReplacementClaim(**payload))
        self.rebuild_worker_registration_projection()
        self.rebuild_claim_projection()
        return self.runtime_materialization_digest()

    # ------------------------------------------------------------------
    # Trace operations (G3, 040 C3)
    # ------------------------------------------------------------------

    def append_trace_entry(self, entry) -> None:
        """Insert a TraceEntry into the trace_events table."""
        with self._conn:
            self._insert_trace_entry(entry)
            self._insert_runtime_record(
                create_runtime_record(
                    "trace.recorded",
                    {"trace": trace_entry_to_dict(entry)},
                )
            )

    def append(self, entry: TraceEntry) -> None:
        """TraceStoreProtocol-compatible append."""
        self.append_trace_entry(entry)

    def new_entry(
        self, contract_id: str, event_type: str, **kwargs: object
    ) -> TraceEntry:
        """TraceStoreProtocol-compatible helper."""
        from aigineering.core.trace import create_entry

        parent_id = kwargs.get("parent_id")
        if parent_id is None:
            existing = self.get_trace_events(contract_id)
            if existing:
                kwargs["parent_id"] = existing[-1].id
        entry = create_entry(
            contract_id=contract_id,
            event_type=event_type,
            sequence=len(self.get_trace_events()),
            **kwargs,  # type: ignore[arg-type]
        )
        self.append_trace_entry(entry)
        return entry

    def _insert_trace_entry(self, entry: TraceEntry) -> None:
        row = self._conn.execute(
            "SELECT * FROM trace_events WHERE id = ?", (entry.id,)
        ).fetchone()
        if row is not None:
            existing = self._row_to_trace_entry(row)
            if trace_effective_payload(existing) == trace_effective_payload(entry):
                return
            raise ImmutableRecordConflict("trace event", entry.id)
        d = trace_entry_to_dict(entry)
        try:
            self._conn.execute(
                """INSERT INTO trace_events (
                    id, parent_id, contract_id, event_type,
                    disclosed_assets, accepted_fragments, accepted_asset_names,
                    rejected_fragments, worker_id, candidate_raw,
                    authority_policy, authority_result, budget_remaining,
                    relation_type, relation_target, timestamp, usage_metadata
                ) VALUES (
                    :id, :parent_id, :contract_id, :event_type,
                    :disclosed_assets, :accepted_fragments, :accepted_asset_names,
                    :rejected_fragments, :worker_id, :candidate_raw,
                    :authority_policy, :authority_result, :budget_remaining,
                    :relation_type, :relation_target, :timestamp, :usage_metadata
                )""",
                {
                    k: json.dumps(v) if isinstance(v, (list, dict, tuple)) else v
                    for k, v in d.items()
                },
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                "SELECT * FROM trace_events WHERE id = ?", (entry.id,)
            ).fetchone()
            if row is not None and trace_effective_payload(
                self._row_to_trace_entry(row)
            ) == trace_effective_payload(entry):
                return
            raise ImmutableRecordConflict("trace event", entry.id) from None

    def get_trace_events(self, contract_id: str | None = None) -> list:
        """Return trace entries, optionally filtered by contract_id."""
        if contract_id is not None:
            cur = self._conn.execute(
                "SELECT * FROM trace_events WHERE contract_id = ? ORDER BY rowid",
                (contract_id,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM trace_events ORDER BY rowid")
        return [self._row_to_trace_entry(row) for row in cur.fetchall()]

    def get_by_contract(self, contract_id: str) -> list[TraceEntry]:
        return self.get_trace_events(contract_id)

    def get_by_event_type(self, event_type: str) -> list[TraceEntry]:
        cur = self._conn.execute(
            "SELECT * FROM trace_events WHERE event_type = ? ORDER BY rowid",
            (event_type,),
        )
        return [self._row_to_trace_entry(row) for row in cur.fetchall()]

    def get_all(self) -> list[TraceEntry]:
        return self.get_trace_events()

    def get_reverse_lineage(self, asset_id: str) -> list[TraceEntry]:
        return [
            entry
            for entry in self.get_trace_events()
            if entry_references_asset(entry, asset_id)
        ]

    def _row_to_trace_entry(self, row: sqlite3.Row):
        """Convert a trace_events row to a TraceEntry."""
        from aigineering.protocol.types import TraceEntry
        import json as _json

        usage_raw = row["usage_metadata"] if "usage_metadata" in row.keys() else None
        usage = _json.loads(usage_raw) if usage_raw else None

        return TraceEntry(
            id=row["id"],
            parent_id=row["parent_id"],
            contract_id=row["contract_id"],
            event_type=row["event_type"],
            disclosed_assets=_json.loads(row["disclosed_assets"] or "[]"),
            accepted_fragments=_json.loads(row["accepted_fragments"] or "[]"),
            accepted_asset_names=_json.loads(row["accepted_asset_names"] or "[]"),
            rejected_fragments=_json.loads(row["rejected_fragments"] or "[]"),
            worker_id=row["worker_id"],
            candidate_raw=row["candidate_raw"],
            authority_policy=row["authority_policy"],
            authority_result=row["authority_result"],
            budget_remaining=row["budget_remaining"],
            relation_type=row["relation_type"],
            relation_target=row["relation_target"],
            timestamp=row["timestamp"],
            usage_metadata=MappingProxyType(usage) if isinstance(usage, dict) else None,
        )

    # ------------------------------------------------------------------
    # Claim persistence (040 C4, G8)
    # ------------------------------------------------------------------

    def persist_claim(
        self,
        claim_id: str,
        contract_id: str,
        worker_id: str,
        lease_until: str,
        status: str = "active",
        package_id: str = "",
        epoch: int = 1,
    ) -> None:
        """Import an immutable worker claim and append its lifecycle facts.

        This compatibility entry point is intentionally insert-only. Runtime
        state transitions use the explicit lifecycle methods below; callers
        cannot rewrite a previously observed claim in place.
        """
        if status not in {"active", "released", "submitted"}:
            raise ValueError(f"unsupported claim status: {status!r}")
        now = now_iso()
        with self._conn:
            existing = self._conn.execute(
                "SELECT claim_id, contract_id, worker_id, lease_until, status, "
                "package_id, epoch FROM worker_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            expected = (
                claim_id,
                contract_id,
                worker_id,
                lease_until,
                status,
                package_id,
                epoch,
            )
            if existing is not None:
                observed = tuple(existing[key] for key in existing.keys())
                if observed == expected:
                    return
                raise ImmutableRecordConflict("worker claim", claim_id)
            self._conn.execute(
                """INSERT INTO worker_claims (
                    claim_id, contract_id, worker_id, lease_until, status,
                    package_id, epoch, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    claim_id,
                    contract_id,
                    worker_id,
                    lease_until,
                    status,
                    package_id,
                    epoch,
                    now,
                    now,
                ),
            )
            granted = create_runtime_record(
                "claim.granted",
                {
                    "claim_id": claim_id,
                    "contract_id": contract_id,
                    "epoch": epoch,
                    "lease_until": lease_until,
                    "package_id": package_id,
                    "worker_id": worker_id,
                },
                recorded_at=now,
            )
            self._insert_runtime_record(granted)
            if status != "active":
                self._insert_runtime_record(
                    create_runtime_record(
                        f"claim.{status}",
                        {
                            "claim_id": claim_id,
                            "contract_id": contract_id,
                            "epoch": epoch,
                            "package_id": package_id,
                            "worker_id": worker_id,
                        },
                        causal_parents=[granted.id],
                        recorded_at=now,
                    )
                )

    def mark_claim_released(self, claim_id: str) -> None:
        """Append a release fact and update the derived claim head."""
        with self._conn:
            row = self._conn.execute(
                "SELECT contract_id, worker_id, epoch, package_id, lease_until "
                "FROM worker_claims WHERE claim_id = ? AND status = 'active'",
                (claim_id,),
            ).fetchone()
            if row is None:
                return
            recorded_at = now_iso()
            self._conn.execute(
                "UPDATE worker_claims SET status = 'released', updated_at = ? "
                "WHERE claim_id = ? AND status = 'active'",
                (recorded_at, claim_id),
            )
            self._insert_runtime_record(
                create_runtime_record(
                    "claim.released",
                    {
                        "claim_id": claim_id,
                        "contract_id": row["contract_id"],
                        "epoch": int(row["epoch"]),
                        "package_id": row["package_id"],
                        "worker_id": row["worker_id"],
                    },
                    causal_parents=[self._claim_granted_record_id(claim_id)],
                    recorded_at=recorded_at,
                )
            )

    def get_claim(self, contract_id: str) -> dict | None:
        """Return the active claim for *contract_id*, or None."""
        cur = self._conn.execute(
            "SELECT claim_id, contract_id, worker_id, lease_until, status, package_id, epoch "
            "FROM worker_claims WHERE contract_id = ? "
            "ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END, rowid DESC LIMIT 1",
            (contract_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "claim_id": row["claim_id"],
            "contract_id": row["contract_id"],
            "worker_id": row["worker_id"],
            "lease_until": row["lease_until"],
            "status": row["status"],
            "package_id": row["package_id"],
            "epoch": int(row["epoch"]),
        }

    def claim_contract(
        self,
        contract_id: str,
        worker_id: str,
        lease_seconds: int = 60,
        package_id: str = "",
        expected_registration_version: str = "",
        runtime_records: tuple[RuntimeRecord, ...] = (),
    ) -> dict | None:
        """Atomically arbitrate contract exclusivity and registered capacity."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claim_id = f"lease:{compute_content_hash(f'{contract_id}|{worker_id}|{now.isoformat()}')}"
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            existing = self._conn.execute(
                "SELECT claim_id, status FROM worker_claims "
                "WHERE contract_id = ? "
                "ORDER BY epoch DESC LIMIT 1",
                (contract_id,),
            ).fetchone()
            if existing is not None:
                self._conn.rollback()
                return None

            registration = self._conn.execute(
                "SELECT enabled, capacity, version FROM worker_registrations "
                "WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if registration is not None:
                active_claims = self._conn.execute(
                    "SELECT COUNT(*) FROM worker_claims "
                    "WHERE worker_id = ? AND status = 'active'",
                    (worker_id,),
                ).fetchone()[0]
                if (
                    not bool(registration["enabled"])
                    or active_claims >= int(registration["capacity"])
                    or (
                        expected_registration_version
                        and registration["version"] != expected_registration_version
                    )
                ):
                    self._conn.rollback()
                    return None

            epoch = int(
                self._conn.execute(
                    "SELECT COALESCE(MAX(epoch), 0) + 1 FROM worker_claims "
                    "WHERE contract_id = ?",
                    (contract_id,),
                ).fetchone()[0]
            )
            timestamp = now_iso()
            if any(
                self.get_runtime_record(record.id) is not None
                for record in runtime_records
            ):
                self._conn.rollback()
                return None
            for record in runtime_records:
                self._insert_runtime_record(record)
            self._conn.execute(
                """INSERT INTO worker_claims (
                    claim_id, contract_id, worker_id, lease_until, status,
                    package_id, epoch, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                (
                    claim_id,
                    contract_id,
                    worker_id,
                    lease_until,
                    package_id,
                    epoch,
                    timestamp,
                    timestamp,
                ),
            )
            self._insert_runtime_record(
                create_runtime_record(
                    "claim.granted",
                    {
                        "claim_id": claim_id,
                        "contract_id": contract_id,
                        "epoch": epoch,
                        "lease_until": lease_until,
                        "package_id": package_id,
                        "worker_id": worker_id,
                    },
                )
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return None
        except sqlite3.OperationalError as exc:
            self._conn.rollback()
            if _is_sqlite_contention(exc):
                return None
            raise
        return self.get_claim(contract_id)

    def renew_claim(
        self,
        claim_id: str,
        epoch: int,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        runtime_records: tuple[RuntimeRecord, ...] = (),
    ) -> dict | None:
        """Renew an active claim only when all fencing identities still match."""
        from datetime import datetime, timedelta, timezone

        if epoch < 1 or lease_seconds < 1:
            return None
        now = datetime.now(timezone.utc)
        deadline = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._conn:
            if any(
                self.get_runtime_record(record.id) is not None
                for record in runtime_records
            ):
                return None
            cursor = self._conn.execute(
                "UPDATE worker_claims SET lease_until = ?, updated_at = ? "
                "WHERE claim_id = ? AND epoch = ? AND worker_id = ? "
                "AND status = 'active' AND lease_until >= ?",
                (deadline, now_iso(), claim_id, epoch, worker_id, now.isoformat()),
            )
            if cursor.rowcount != 1:
                return None
            for record in runtime_records:
                self._insert_runtime_record(record)
            row = self._conn.execute(
                "SELECT contract_id, worker_id, epoch, package_id, lease_until "
                "FROM worker_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            self._insert_runtime_record(
                create_runtime_record(
                    "claim.renewed",
                    {
                        "claim_id": claim_id,
                        "epoch": epoch,
                        "lease_until": deadline,
                        "worker_id": worker_id,
                    },
                    causal_parents=[self._claim_granted_record_id(claim_id)],
                )
            )
        return self.get_claim(row["contract_id"]) if row is not None else None

    def mark_claim_submitted(self, claim_id: str) -> None:
        with self._conn:
            row = self._conn.execute(
                "SELECT contract_id, worker_id, epoch, package_id "
                "FROM worker_claims WHERE claim_id = ? AND status = 'active'",
                (claim_id,),
            ).fetchone()
            if row is None:
                return
            self._conn.execute(
                "UPDATE worker_claims SET status = 'submitted', updated_at = ? "
                "WHERE claim_id = ? AND status = 'active'",
                (now_iso(), claim_id),
            )
            self._insert_runtime_record(
                create_runtime_record(
                    "claim.submitted",
                    {
                        "claim_id": claim_id,
                        "contract_id": row["contract_id"],
                        "epoch": int(row["epoch"]),
                        "package_id": row["package_id"],
                        "worker_id": row["worker_id"],
                    },
                    causal_parents=[self._claim_granted_record_id(claim_id)],
                )
            )

    def _claim_granted_record_id(self, claim_id: str) -> str:
        """Return the canonical causal root for one claim lifecycle."""
        rows = self._conn.execute(
            "SELECT record_id, payload_json FROM runtime_records "
            "WHERE record_type = 'claim.granted' ORDER BY revision DESC"
        ).fetchall()
        for row in rows:
            if json.loads(row["payload_json"]).get("claim_id") == claim_id:
                return str(row["record_id"])
        raise RuntimeError(f"claim {claim_id!r} has no immutable grant fact")

    def rebuild_claim_projection(self) -> None:
        """Rebuild the transactional claim head from immutable claim facts."""
        records = [
            record
            for _, record in self.scan_runtime_records()
            if record.record_type.startswith("claim.")
        ]
        with self._conn:
            self._conn.execute("DELETE FROM worker_claims")
            for record in records:
                payload = record.payload
                if record.record_type == "claim.granted":
                    self._conn.execute(
                        """INSERT INTO worker_claims (
                            claim_id, contract_id, worker_id, lease_until, status,
                            package_id, epoch, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                        (
                            payload["claim_id"],
                            payload["contract_id"],
                            payload["worker_id"],
                            payload["lease_until"],
                            payload["package_id"],
                            int(payload["epoch"]),
                            record.recorded_at,
                            record.recorded_at,
                        ),
                    )
                elif record.record_type == "claim.renewed":
                    self._conn.execute(
                        "UPDATE worker_claims SET lease_until = ?, updated_at = ? "
                        "WHERE claim_id = ? AND epoch = ?",
                        (
                            payload["lease_until"],
                            record.recorded_at,
                            payload["claim_id"],
                            int(payload["epoch"]),
                        ),
                    )
                elif record.record_type in {
                    "claim.expired",
                    "claim.released",
                    "claim.submitted",
                }:
                    status = record.record_type.removeprefix("claim.")
                    self._conn.execute(
                        "UPDATE worker_claims SET status = ?, updated_at = ? "
                        "WHERE claim_id = ? AND epoch = ?",
                        (
                            status,
                            record.recorded_at,
                            payload["claim_id"],
                            int(payload["epoch"]),
                        ),
                    )

    def get_idempotency(self, contract_id: str, idempotency_key: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT result_json FROM idempotency_records "
            "WHERE contract_id = ? AND idempotency_key = ?",
            (contract_id, idempotency_key),
        )
        row = cur.fetchone()
        return json.loads(row["result_json"]) if row else None

    def has_any_idempotency(self, contract_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM idempotency_records WHERE contract_id = ? LIMIT 1",
            (contract_id,),
        )
        return cur.fetchone() is not None

    def set_idempotency(
        self, contract_id: str, idempotency_key: str, result: dict
    ) -> None:
        existing = self.get_idempotency(contract_id, idempotency_key)
        if existing is not None:
            if existing == result:
                return
            raise ImmutableRecordConflict(
                "idempotency record", f"{contract_id}:{idempotency_key}"
            )
        try:
            self._conn.execute(
                "INSERT INTO idempotency_records "
                "(contract_id, idempotency_key, result_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    contract_id,
                    idempotency_key,
                    json.dumps(result, sort_keys=True),
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            if self.get_idempotency(contract_id, idempotency_key) == result:
                return
            raise ImmutableRecordConflict(
                "idempotency record", f"{contract_id}:{idempotency_key}"
            ) from None

    def _lock_worker_key_binding(self, worker_id: str, key_id: str) -> None:
        """Acquire writer arbitration and verify the current routing identity."""
        # A SELECT does not start SQLite's write transaction. This no-op write
        # prevents another replica from rebinding the worker before commitment.
        self._conn.execute(
            "UPDATE worker_registrations SET updated_at = updated_at "
            "WHERE worker_id = ?",
            (worker_id,),
        )
        registration = self._conn.execute(
            "SELECT actor_id, key_id, enabled FROM worker_registrations "
            "WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()
        if (
            registration is None
            or not bool(registration["enabled"])
            or registration["actor_id"] != worker_id
            or registration["key_id"] != key_id
        ):
            raise WorkerBindingConflict(
                "worker actor-key binding changed during submit"
            )

    def commit_worker_invocation_failure(
        self,
        *,
        trace_entry: TraceEntry,
        runtime_records: tuple[RuntimeRecord, ...],
        claim_id: str,
        worker_id: str,
        package_id: str,
        claim_epoch: int,
    ) -> bool:
        """Atomically record provider failure, terminal fact, and fenced release."""
        with self._conn:
            self._insert_trace_entry(trace_entry)
            for record in runtime_records:
                self._insert_runtime_record(record)
            recorded_at = now_iso()
            cursor = self._conn.execute(
                "UPDATE worker_claims SET status = 'released', updated_at = ? "
                "WHERE claim_id = ? AND status = 'active' AND worker_id = ? "
                "AND package_id = ? AND epoch = ?",
                (
                    recorded_at,
                    claim_id,
                    worker_id,
                    package_id,
                    claim_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError(
                    "active worker claim predicate failed during invocation failure"
                )
            claim_row = self._conn.execute(
                "SELECT contract_id, worker_id, epoch, package_id "
                "FROM worker_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            failure_parents = [
                record.id
                for record in runtime_records
                if record.record_type == "worker.invocation_failed"
            ]
            self._insert_runtime_record(
                create_runtime_record(
                    "claim.released",
                    {
                        "claim_id": claim_id,
                        "contract_id": claim_row["contract_id"],
                        "epoch": int(claim_row["epoch"]),
                        "package_id": claim_row["package_id"],
                        "worker_id": claim_row["worker_id"],
                    },
                    causal_parents=[self._claim_granted_record_id(claim_id)]
                    + failure_parents,
                    recorded_at=recorded_at,
                )
            )
        return True

    def commit_claim_expiration(
        self,
        *,
        trace_entry: TraceEntry,
        runtime_records: tuple[RuntimeRecord, ...],
        claim_id: str,
        claim_epoch: int,
        expected_lease_until: str,
        observed_at: str,
    ) -> bool:
        """Atomically turn an expired lease into durable failure facts."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE worker_claims SET status = 'expired', updated_at = ? "
                "WHERE claim_id = ? AND epoch = ? AND status = 'active' "
                "AND lease_until = ?",
                (observed_at, claim_id, claim_epoch, expected_lease_until),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_trace_entry(trace_entry)
            for record in runtime_records:
                self._insert_runtime_record(record)
            if not any(
                record.record_type == "claim.expired" for record in runtime_records
            ):
                raise ValueError("claim expiration commit requires claim.expired fact")
        return True

    def commit_ingress_batch(
        self,
        accepted_assets: list[Asset],
        trace_entries: list[TraceEntry],
        *,
        contract: Contract | None = None,
        contracts: tuple[Contract, ...] = (),
        reducer_callback: Callable[[], list[TraceEntry]] | None = None,
        runtime_records: tuple[RuntimeRecord, ...] = (),
        claim_binding=None,
        candidate_actor_id: str = "",
        candidate_key_id: str = "",
        candidate_id: str = "",
    ) -> None:
        with self._conn:
            # Conditional allowance/key/claim checks must observe the same
            # single-writer snapshot as their inserts, even for batches that
            # contain no Contract or Asset row to acquire the lock first.
            self._conn.execute("BEGIN IMMEDIATE")
            if self._candidate_already_committed(candidate_id):
                return
            if claim_binding is not None and self._validate_claim_binding(
                claim_binding,
                candidate_actor_id,
                candidate_key_id,
                candidate_id,
            ):
                return
            declarations = ((contract,) if contract is not None else ()) + tuple(
                contracts
            )
            self._validate_output_qualification_records(runtime_records)
            for declaration in declarations:
                self._insert_contract(declaration)
            self._validate_allowance_records(runtime_records)
            self._insert_ingress_assets(accepted_assets)
            check_crash_point("after_asset_before_trace")
            if reducer_callback is not None:
                reducer_traces: list[TraceEntry] = reducer_callback()
                trace_entries = list(trace_entries) + reducer_traces
            for entry in trace_entries:
                self._insert_trace_entry(entry)
            check_crash_point("after_trace_before_runtime_records")
            for record in runtime_records:
                self._insert_runtime_record(record)
            if claim_binding is not None:
                self._finalize_claim_submission(
                    claim_binding,
                    candidate_actor_id,
                    candidate_id,
                    runtime_records,
                )

    def _candidate_already_committed(self, candidate_id: str) -> bool:
        return bool(
            candidate_id
            and self._conn.execute(
                "SELECT 1 FROM runtime_records WHERE record_type = "
                "'candidate.committed' AND "
                "json_extract(payload_json, '$.candidate_id') = ? LIMIT 1",
                (candidate_id,),
            ).fetchone()
        )

    def _validate_claim_binding(
        self, claim_binding, actor_id: str, key_id: str, candidate_id: str
    ) -> bool:
        """Validate the claim fence; return True for an exact closed replay."""
        if actor_id == "" or key_id == "":
            raise ValueError("claim-bound commitment requires actor key binding")
        self._lock_worker_key_binding(actor_id, key_id)
        existing = self._conn.execute(
            "SELECT contract_id, worker_id, epoch, package_id, status, lease_until "
            "FROM worker_claims WHERE claim_id = ?",
            (claim_binding.claim_id,),
        ).fetchone()
        if existing is None:
            raise sqlite3.IntegrityError("claim-bound Candidate has unknown claim")
        if existing["status"] == "submitted" and any(
            record.payload.get("claim_id") == claim_binding.claim_id
            and record.payload.get("candidate_id") == candidate_id
            for _, record in self.scan_runtime_records(record_type="claim.submitted")
        ):
            return True
        if (
            existing["status"] != "active"
            or existing["contract_id"] != claim_binding.contract_id
            or existing["worker_id"] != actor_id
            or int(existing["epoch"]) != claim_binding.claim_epoch
            or existing["package_id"] != claim_binding.package_id
            or existing["lease_until"] < now_iso()
        ):
            raise sqlite3.IntegrityError(
                "active worker claim predicate failed during Candidate commit"
            )
        return False

    def _validate_output_qualification_records(
        self, runtime_records: tuple[RuntimeRecord, ...]
    ) -> None:
        if not any(
            record.record_type == "output.qualified" for record in runtime_records
        ):
            return
        from aigineering.core.acceptance import validate_output_qualification_commit

        validate_output_qualification_commit(
            tuple(record for _, record in self.scan_runtime_records()),
            runtime_records,
        )

    def _validate_allowance_records(
        self, runtime_records: tuple[RuntimeRecord, ...]
    ) -> None:
        if not any(
            record.record_type in {"allowance.reserved", "allowance.extinguished"}
            for record in runtime_records
        ):
            return
        from aigineering.core.causal_allowance import validate_allowance_commit

        pending_ids = {record.id for record in runtime_records}
        existing_records = tuple(
            record
            for _, record in self.scan_runtime_records()
            if record.id not in pending_ids
        )
        validate_allowance_commit(
            tuple(self.get_all_contracts()), existing_records, runtime_records
        )

    def _insert_ingress_assets(self, accepted_assets: list[Asset]) -> None:
        for asset in accepted_assets:
            if not asset.signed_by or not verify_asset_seal(asset):
                raise ValueError(
                    f"Asset '{asset.id}' rejected — "
                    "missing or invalid canonical seal "
                    f"(signed_by={asset.signed_by!r})"
                )
            self._insert_asset(asset)

    def _finalize_claim_submission(
        self,
        claim_binding,
        actor_id: str,
        candidate_id: str,
        runtime_records: tuple[RuntimeRecord, ...],
    ) -> None:
        committed_at = now_iso()
        cursor = self._conn.execute(
            "UPDATE worker_claims SET status = 'submitted', updated_at = ? "
            "WHERE claim_id = ? AND status = 'active' AND worker_id = ? "
            "AND contract_id = ? AND package_id = ? AND epoch = ? "
            "AND lease_until >= ?",
            (
                committed_at,
                claim_binding.claim_id,
                actor_id,
                claim_binding.contract_id,
                claim_binding.package_id,
                claim_binding.claim_epoch,
                committed_at,
            ),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("claim changed during Candidate commitment")
        decision_parents = [
            record.id
            for record in runtime_records
            if record.record_type == "candidate.committed"
        ]
        self._insert_runtime_record(
            create_runtime_record(
                "claim.submitted",
                {
                    "candidate_id": candidate_id,
                    "claim_id": claim_binding.claim_id,
                    "contract_id": claim_binding.contract_id,
                    "epoch": claim_binding.claim_epoch,
                    "package_id": claim_binding.package_id,
                    "worker_id": actor_id,
                },
                causal_parents=[self._claim_granted_record_id(claim_binding.claim_id)]
                + decision_parents,
            )
        )

    # ------------------------------------------------------------------
    # Replacement claim persistence
    # ------------------------------------------------------------------

    def add_replacement_claim(self, claim) -> None:
        with self._conn:
            self._insert_replacement_claim(claim)

    def _insert_replacement_claim(self, claim) -> None:
        row = self._conn.execute(
            "SELECT * FROM claims WHERE id = ?", (claim.id,)
        ).fetchone()
        if row is not None:
            existing = self._row_to_replacement_claim(row)
            if existing == claim:
                return
            raise ImmutableRecordConflict("replacement claim", claim.id)
        try:
            self._conn.execute(
                """INSERT INTO claims (
                    id, source_asset_id, replacement_asset_id,
                    definition_hash, claim_type, signed_by,
                    provenance_seal, lineage_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    claim.id,
                    claim.source_asset_id,
                    claim.replacement_asset_id,
                    claim.definition_hash,
                    claim.claim_type,
                    claim.signed_by,
                    claim.provenance_seal,
                    claim.lineage_id,
                ),
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                "SELECT * FROM claims WHERE id = ?", (claim.id,)
            ).fetchone()
            if row is not None and self._row_to_replacement_claim(row) == claim:
                return
            raise ImmutableRecordConflict("replacement claim", claim.id) from None

    def commit_replacement_claim(self, claim, trace_entry: TraceEntry) -> None:
        """Persist a replacement claim and its audit trace atomically."""
        with self._conn:
            self._insert_replacement_claim(claim)
            check_crash_point("after_replacement_claim_before_trace")
            self._insert_trace_entry(trace_entry)
            claim_record = create_runtime_record(
                "replacement.claimed",
                {"claim": replacement_claim_payload(claim)},
            )
            self._insert_runtime_record(claim_record)
            self._insert_runtime_record(
                create_runtime_record(
                    "trace.recorded",
                    {"trace": trace_entry_to_dict(trace_entry)},
                    causal_parents=[claim_record.id],
                )
            )

    def get_claims_by_definition(self, definition_hash: str) -> list:
        rows = self._conn.execute(
            "SELECT * FROM claims WHERE definition_hash = ?",
            (definition_hash,),
        ).fetchall()
        return [self._row_to_replacement_claim(r) for r in rows]

    def get_claims_for_asset(self, asset_id: str) -> list:
        rows = self._conn.execute(
            "SELECT * FROM claims WHERE source_asset_id = ?",
            (asset_id,),
        ).fetchall()
        return [self._row_to_replacement_claim(r) for r in rows]

    @staticmethod
    def _row_to_replacement_claim(row: sqlite3.Row):
        from aigineering.protocol.types import ReplacementClaim

        return ReplacementClaim(
            id=row["id"],
            source_asset_id=row["source_asset_id"],
            replacement_asset_id=row["replacement_asset_id"],
            definition_hash=row["definition_hash"],
            claim_type=row["claim_type"],
            signed_by=row["signed_by"],
            provenance_seal=row["provenance_seal"],
            lineage_id=row["lineage_id"],
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
