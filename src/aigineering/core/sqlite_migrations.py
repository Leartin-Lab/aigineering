"""SQLite schema initialization and historical data migrations."""

from __future__ import annotations

import json
import sqlite3

from aigineering.core.activation import activation_names
from aigineering.core.asset_versions import replacement_claim_payload
from aigineering.core.ids import now_iso
from aigineering.core.sqlite_schema import (
    DDL_CREATE_ACTIVATION_REFS,
    DDL_CREATE_DECLARED_OUTPUTS,
    DDL_CREATE_IDEMPOTENCY,
    DDL_CREATE_RUNTIME_LIFECYCLE,
    DDL_CREATE_RUNTIME_RECORDS,
    DDL_CREATE_SCHEMA_VERSION,
    DDL_CREATE_WORKER_CLAIMS,
    DDL_CREATE_WORKER_REGISTRATIONS,
    DDL_CREATE_ASSET_CONTENTS,
    DDL_CREATE_ASSET_DEFINITIONS,
    DDL_CREATE_DEFINITION_CONTENT_ASSERTIONS,
    DDL_INDEXES,
    TABLE_DDL,
)
from aigineering.core.asset_graph_facts import legacy_asset_graph_record
from aigineering.core.worker_routing import worker_registration_record
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.wire import (
    asset_to_dict,
    contract_to_dict,
    trace_entry_to_dict,
)

CURRENT_SCHEMA_VERSION = 16


class SQLiteMigrator:
    """Own schema evolution while the Store remains one operational facade."""

    def __init__(self, connection: sqlite3.Connection, store) -> None:
        self._conn = connection
        self._store = store

    def create_tables(self) -> None:
        for ddl in TABLE_DDL:
            self._conn.execute(ddl)
        for idx_ddl in DDL_INDEXES:
            self._conn.execute(idx_ddl)
        self._conn.commit()

    def current_schema_version(self) -> int:
        cur = self._conn.execute("SELECT MAX(version) FROM schema_version")
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else 0

    def _ensure_contract_columns(self) -> None:
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(contracts)").fetchall()
        }
        if "minting_authority" not in existing:
            self._conn.execute(
                "ALTER TABLE contracts ADD COLUMN minting_authority TEXT NOT NULL DEFAULT '[]'"
            )
        if "sensitive_input_policy" not in existing:
            self._conn.execute(
                "ALTER TABLE contracts ADD COLUMN sensitive_input_policy TEXT"
            )
        if "worker_capabilities" not in existing:
            self._conn.execute(
                "ALTER TABLE contracts ADD COLUMN worker_capabilities TEXT NOT NULL DEFAULT '[]'"
            )
        if "worker_pools" not in existing:
            self._conn.execute(
                "ALTER TABLE contracts ADD COLUMN worker_pools TEXT NOT NULL DEFAULT '[]'"
            )
        if "acceptance_policy" not in existing:
            self._conn.execute(
                "ALTER TABLE contracts ADD COLUMN acceptance_policy TEXT"
            )
        if "context_asset_ids" not in existing:
            self._conn.execute(
                "ALTER TABLE contracts "
                "ADD COLUMN context_asset_ids TEXT NOT NULL DEFAULT '[]'"
            )

    def _record_schema_version(self, version: int) -> None:
        self._conn.execute("DELETE FROM schema_version")
        self._conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, now_iso()),
        )

    def run_migrations(self, current: int) -> None:
        with self._conn:
            if current < 1:
                self._record_schema_version(1)
            if current < 2:
                self._migrate_to_v2()
                self._record_schema_version(2)
            if current < 3:
                self._migrate_to_v3()
                self._record_schema_version(3)
            if current < 4:
                self._migrate_to_v4()
                self._record_schema_version(4)
            if current < 5:
                self._migrate_to_v5()
                self._record_schema_version(5)
            if current < 6:
                self._migrate_to_v6()
                self._record_schema_version(6)
            if current < 7:
                self._migrate_to_v7()
                self._record_schema_version(7)
            if current < 8:
                self._migrate_to_v8()
                self._record_schema_version(8)
            if current < 9:
                self._migrate_to_v9()
                self._record_schema_version(9)
            if current < 10:
                self._migrate_to_v10()
                self._record_schema_version(10)
            if current < 11:
                self._migrate_to_v11()
                self._record_schema_version(11)
            if current < 12:
                self._migrate_to_v12()
                self._record_schema_version(12)
            if current < 13:
                self._migrate_to_v13()
                self._record_schema_version(13)
            if current < 14:
                self._migrate_to_v14()
                self._record_schema_version(14)
            if current < 15:
                self._migrate_to_v15()
                self._record_schema_version(15)
            if current < 16:
                self._migrate_to_v16()
                self._record_schema_version(16)

    def _migrate_to_v2(self) -> None:
        """Add 040 transactional worker state and contract authority metadata."""
        self._ensure_contract_columns()
        for ddl in (DDL_CREATE_WORKER_CLAIMS, DDL_CREATE_IDEMPOTENCY):
            self._conn.execute(ddl)
        for idx_ddl in DDL_INDEXES:
            self._conn.execute(idx_ddl)

    def _migrate_to_v3(self) -> None:
        """Persist trace usage metadata for LLM token/cost accounting."""
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(trace_events)").fetchall()
        }
        if "usage_metadata" not in existing:
            self._conn.execute(
                "ALTER TABLE trace_events ADD COLUMN usage_metadata TEXT"
            )

    def _migrate_to_v4(self) -> None:
        """Add dependency/output index tables and backfill from existing
        contracts so upgraded databases do not return false negatives."""
        for ddl in (
            DDL_CREATE_ACTIVATION_REFS,
            DDL_CREATE_DECLARED_OUTPUTS,
            DDL_CREATE_RUNTIME_LIFECYCLE,
        ):
            self._conn.execute(ddl)
        for idx_ddl in DDL_INDEXES:
            self._conn.execute(idx_ddl)

        # Backfill: register activation refs and declared outputs for every
        # contract already in the database.
        import json as _json

        rows = list(self._conn.execute("SELECT id, activation, outputs FROM contracts"))
        for row in rows:
            contract_id = row["id"]
            activation: str = row["activation"] or ""
            outputs_raw: str = row["outputs"] or "[]"

            for asset_name in activation_names(activation):
                self._conn.execute(
                    "INSERT OR IGNORE INTO contract_activation_refs "
                    "(contract_id, asset_name) VALUES (?, ?)",
                    (contract_id, asset_name),
                )
            try:
                declared = _json.loads(outputs_raw)
                if isinstance(declared, list):
                    for output_name in declared:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO contract_declared_outputs "
                            "(contract_id, output_name) VALUES (?, ?)",
                            (contract_id, str(output_name)),
                        )
            except _json.JSONDecodeError:
                pass

    def _migrate_to_v5(self) -> None:
        """Persist routing constraints and trusted worker registrations."""
        self._ensure_contract_columns()
        self._conn.execute(DDL_CREATE_WORKER_REGISTRATIONS)
        for idx_ddl in DDL_INDEXES:
            self._conn.execute(idx_ddl)

    def _migrate_to_v6(self) -> None:
        """Add and backfill the append-only runtime-record envelope."""
        self._conn.execute(DDL_CREATE_RUNTIME_RECORDS)
        for idx_ddl in DDL_INDEXES:
            self._conn.execute(idx_ddl)
        for contract in self._store.get_all_contracts():
            self._store._insert_runtime_record(
                create_runtime_record(
                    "contract.declared", {"contract": contract_to_dict(contract)}
                )
            )
        for asset in self._store.get_all_assets():
            self._store._insert_runtime_record(
                create_runtime_record(
                    "asset.committed",
                    {"asset": asset_to_dict(asset), "contract_id": asset.created_by},
                )
            )
        for entry in self._store.get_all():
            self._store._insert_runtime_record(
                create_runtime_record(
                    "trace.recorded", {"trace": trace_entry_to_dict(entry)}
                )
            )
        for row in self._conn.execute("SELECT * FROM claims ORDER BY id").fetchall():
            claim = self._store._row_to_replacement_claim(row)
            self._store._insert_runtime_record(
                create_runtime_record(
                    "replacement.claimed",
                    {"claim": replacement_claim_payload(claim)},
                )
            )
        for row in self._conn.execute(
            "SELECT contract_id, idempotency_key, result_json FROM idempotency_records"
        ).fetchall():
            self._store._insert_runtime_record(
                create_runtime_record(
                    "idempotency.bound",
                    {
                        "contract_id": row["contract_id"],
                        "key": row["idempotency_key"],
                        "result": json.loads(row["result_json"]),
                    },
                )
            )
        for registration in self._store.get_worker_registrations():
            self._store._insert_runtime_record(worker_registration_record(registration))

    def _migrate_to_v7(self) -> None:
        """Add monotonic fencing epochs to the derived claim head."""
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(worker_claims)")
        }
        if "epoch" not in columns:
            self._conn.execute(
                "ALTER TABLE worker_claims ADD COLUMN epoch INTEGER NOT NULL DEFAULT 1"
            )
        rows = self._conn.execute(
            "SELECT * FROM worker_claims ORDER BY rowid"
        ).fetchall()
        for row in rows:
            granted = create_runtime_record(
                "claim.granted",
                {
                    "claim_id": row["claim_id"],
                    "contract_id": row["contract_id"],
                    "epoch": int(row["epoch"]),
                    "lease_until": row["lease_until"],
                    "package_id": row["package_id"],
                    "worker_id": row["worker_id"],
                },
                recorded_at=row["created_at"],
            )
            self._store._insert_runtime_record(granted)
            if row["status"] == "submitted":
                self._store._insert_runtime_record(
                    create_runtime_record(
                        "claim.submitted",
                        {
                            "claim_id": row["claim_id"],
                            "contract_id": row["contract_id"],
                            "epoch": int(row["epoch"]),
                            "package_id": row["package_id"],
                            "worker_id": row["worker_id"],
                        },
                        causal_parents=[granted.id],
                        recorded_at=row["updated_at"],
                    )
                )

    def _migrate_to_v8(self) -> None:
        """Enforce exactly one immutable Genesis record per Store."""
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_records_one_genesis "
            "ON runtime_records(record_type) WHERE record_type = 'domain.genesis'"
        )

    def _migrate_to_v9(self) -> None:
        """Enforce one immutable terminal fact per Contract."""
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_runtime_records_one_terminal_per_contract "
            "ON runtime_records(json_extract(payload_json, '$.contract_id')) "
            "WHERE record_type = 'lifecycle.terminal'"
        )

    def _migrate_to_v10(self) -> None:
        """Prevent actor/key identities from being rebound to new public keys."""
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_records_one_actor_key "
            "ON runtime_records("
            "json_extract(payload_json, '$.actor_id'), "
            "json_extract(payload_json, '$.key_id')) "
            "WHERE record_type = 'actor.authorized'"
        )

    def _migrate_to_v11(self) -> None:
        """Make actor-key revocation a single immutable decision."""
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_runtime_records_one_actor_revocation "
            "ON runtime_records("
            "json_extract(payload_json, '$.actor_id'), "
            "json_extract(payload_json, '$.key_id')) "
            "WHERE record_type = 'actor.revoked'"
        )

    def _migrate_to_v12(self) -> None:
        """Bind the worker routing projection to Candidate actor keys."""
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(worker_registrations)")
        }
        if "actor_id" not in columns:
            self._conn.execute(
                "ALTER TABLE worker_registrations "
                "ADD COLUMN actor_id TEXT NOT NULL DEFAULT ''"
            )
        if "key_id" not in columns:
            self._conn.execute(
                "ALTER TABLE worker_registrations "
                "ADD COLUMN key_id TEXT NOT NULL DEFAULT ''"
            )

    def _migrate_to_v13(self) -> None:
        """Persist Contract-bound output acceptance policy."""
        self._ensure_contract_columns()

    def _migrate_to_v14(self) -> None:
        """Add rebuildable definition/content graph indexes and legacy facts."""
        for ddl in (
            DDL_CREATE_ASSET_CONTENTS,
            DDL_CREATE_ASSET_DEFINITIONS,
            DDL_CREATE_DEFINITION_CONTENT_ASSERTIONS,
        ):
            self._conn.execute(ddl)
        for index in DDL_INDEXES:
            self._conn.execute(index)

        genesis_records = self._store.scan_runtime_records(record_type="domain.genesis")
        domain_id = (
            str(genesis_records[0][1].payload["manifest"]["id"])
            if genesis_records
            else "legacy:unbound"
        )
        asset_records = {
            str(record.payload["asset"]["id"]): record.id
            for _, record in self._store.scan_runtime_records(
                record_type="asset.committed"
            )
        }
        for asset in self._store.get_all_assets():
            record = legacy_asset_graph_record(
                asset,
                domain_id=domain_id,
                causal_parent=asset_records.get(asset.id, ""),
            )
            self._store._insert_runtime_record(record)

    def _migrate_to_v15(self) -> None:
        """Bind exact context Asset references into v4 Contracts."""
        self._ensure_contract_columns()

    def _migrate_to_v16(self) -> None:
        """Persist deterministic derivation evidence for slice claims."""
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(claims)")
        }
        if "derivation_version" not in columns:
            self._conn.execute(
                "ALTER TABLE claims ADD COLUMN derivation_version "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "range_spec" not in columns:
            self._conn.execute(
                "ALTER TABLE claims ADD COLUMN range_spec TEXT NOT NULL DEFAULT ''"
            )


def initialize_sqlite_schema(connection: sqlite3.Connection, store) -> None:
    """Create current tables and transactionally migrate historical rows."""
    connection.execute(DDL_CREATE_SCHEMA_VERSION)
    connection.commit()
    migrator = SQLiteMigrator(connection, store)
    current = migrator.current_schema_version()
    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"SQLite schema version {current} is newer than supported "
            f"version {CURRENT_SCHEMA_VERSION}. Refusing to open — "
            "this build cannot read a newer database."
        )
    migrator.create_tables()
    migrator.run_migrations(current)
    store.rebuild_asset_graph_projection()


def current_schema_version(connection: sqlite3.Connection) -> int:
    """Read the applied schema version without exposing migration internals."""
    return SQLiteMigrator(connection, None).current_schema_version()
