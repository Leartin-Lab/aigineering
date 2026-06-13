"""SQLite transactional store for Assets, Contracts, and related entities.

Implements StoreProtocol with WAL-mode SQLite for concurrent reads,
schema versioning with migration hooks, and index coverage for all
lookup fields.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from aigineering.core.provenance import verify_asset_seal
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import asset_to_dict, contract_to_dict

_logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""

_DDL_CREATE_ASSETS = """
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text',
    created_by TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'system',
    trust_tier TEXT NOT NULL DEFAULT 'untrusted',
    minted_by TEXT NOT NULL DEFAULT '',
    source_uri TEXT NOT NULL DEFAULT '',
    signed_by TEXT NOT NULL DEFAULT '',
    provenance_seal TEXT NOT NULL DEFAULT '',
    promptable INTEGER NOT NULL DEFAULT 1,
    disclosure_view TEXT NOT NULL DEFAULT 'original',
    definition_hash TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    keep_flag INTEGER NOT NULL DEFAULT 0,
    tombstoned INTEGER NOT NULL DEFAULT 0,
    tombstoned_at TEXT,
    lineage_id TEXT NOT NULL DEFAULT ''
)
"""

_DDL_CREATE_CONTRACTS = """
CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    inputs TEXT NOT NULL DEFAULT '[]',
    outputs TEXT NOT NULL DEFAULT '[]',
    activation TEXT NOT NULL DEFAULT '',
    budget INTEGER NOT NULL DEFAULT 0,
    tool_scope TEXT NOT NULL DEFAULT '[]',
    labels TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'human'
)
"""

_DDL_CREATE_TRACE_EVENTS = """
CREATE TABLE IF NOT EXISTS trace_events (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    contract_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    disclosed_assets TEXT NOT NULL DEFAULT '[]',
    accepted_fragments TEXT NOT NULL DEFAULT '[]',
    accepted_asset_names TEXT NOT NULL DEFAULT '[]',
    rejected_fragments TEXT NOT NULL DEFAULT '[]',
    worker_id TEXT,
    candidate_raw TEXT,
    authority_policy TEXT,
    authority_result TEXT,
    budget_remaining INTEGER NOT NULL DEFAULT 0,
    relation_type TEXT,
    relation_target TEXT,
    timestamp TEXT NOT NULL DEFAULT ''
)
"""

_DDL_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    root_contract_id TEXT NOT NULL DEFAULT '',
    contract_ids TEXT NOT NULL DEFAULT '[]',
    asset_ids TEXT NOT NULL DEFAULT '[]',
    trace_ids TEXT NOT NULL DEFAULT '[]',
    config_snapshot TEXT NOT NULL DEFAULT '{}',
    worker_snapshot TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT ''
)
"""

_DDL_CREATE_CLAIMS = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    source_asset_id TEXT NOT NULL DEFAULT '',
    replacement_asset_id TEXT NOT NULL DEFAULT '',
    definition_hash TEXT NOT NULL DEFAULT '',
    claim_type TEXT NOT NULL DEFAULT '',
    signed_by TEXT NOT NULL DEFAULT '',
    provenance_seal TEXT NOT NULL DEFAULT '',
    lineage_id TEXT NOT NULL DEFAULT ''
)
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_assets_definition_hash ON assets(definition_hash)",
    "CREATE INDEX IF NOT EXISTS idx_assets_content_hash ON assets(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_assets_lineage_id ON assets(lineage_id)",
    "CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(name)",
    "CREATE INDEX IF NOT EXISTS idx_assets_created_by ON assets(created_by)",
    "CREATE INDEX IF NOT EXISTS idx_assets_tombstoned ON assets(tombstoned)",
    "CREATE INDEX IF NOT EXISTS idx_contracts_parent_id ON contracts(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_trace_events_contract_id ON trace_events(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_trace_events_event_type ON trace_events(event_type)",
]

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

        self._create_tables()
        self._run_migrations()

    # ------------------------------------------------------------------
    # Schema lifecycle
    # ------------------------------------------------------------------

    def _create_tables(self) -> None:
        for ddl in [
            _DDL_CREATE_SCHEMA_VERSION,
            _DDL_CREATE_ASSETS,
            _DDL_CREATE_CONTRACTS,
            _DDL_CREATE_TRACE_EVENTS,
            _DDL_CREATE_SESSIONS,
            _DDL_CREATE_CLAIMS,
        ]:
            self._conn.execute(ddl)
        for idx_ddl in _DDL_INDEXES:
            self._conn.execute(idx_ddl)
        self._conn.commit()

    def _run_migrations(self) -> None:
        cur = self._conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = cur.fetchone()
        current = row[0] if row and row[0] is not None else 0

        if current < 1:
            from aigineering.core.ids import now_iso

            self._conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (1, now_iso()),
            )
            self._conn.commit()

    @property
    def schema_version(self) -> int:
        cur = self._conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else 0

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
            origin=row["origin"],
        )

    # ------------------------------------------------------------------
    # StoreProtocol: assets
    # ------------------------------------------------------------------

    def add_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"G3/N-P1.6: Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        d = asset_to_dict(asset)
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO assets (
                    id, name, content, content_type, created_by,
                    origin, trust_tier, minted_by, source_uri,
                    signed_by, provenance_seal, promptable, disclosure_view,
                    definition_hash, content_hash,
                    keep_flag, tombstoned, tombstoned_at, lineage_id
                ) VALUES (
                    :id, :name, :content, :content_type, :created_by,
                    :origin, :trust_tier, :minted_by, :source_uri,
                    :signed_by, :provenance_seal, :promptable, :disclosure_view,
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

    def get_asset(self, asset_id: str) -> Optional[Asset]:
        cur = self._conn.execute(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        )
        row = cur.fetchone()
        return self._row_to_asset(row) if row else None

    def get_assets_by_name(self, name: str) -> list[Asset]:
        cur = self._conn.execute(
            "SELECT * FROM assets WHERE name = ?", (name,)
        )
        return [self._row_to_asset(row) for row in cur.fetchall()]

    def has_asset_named(self, name: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM assets WHERE name = ? LIMIT 1", (name,)
        )
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

    def add_contract(self, contract: Contract) -> None:
        d = contract_to_dict(contract)
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO contracts (
                    id, parent_id, name, description,
                    inputs, outputs, activation, budget,
                    tool_scope, labels, origin
                ) VALUES (
                    :id, :parent_id, :name, :description,
                    :inputs, :outputs, :activation, :budget,
                    :tool_scope, :labels, :origin
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
                    "origin": d["origin"],
                },
            )

    def get_contract(self, contract_id: str) -> Optional[Contract]:
        cur = self._conn.execute(
            "SELECT * FROM contracts WHERE id = ?", (contract_id,)
        )
        row = cur.fetchone()
        return self._row_to_contract(row) if row else None

    def get_all_contracts(self) -> list[Contract]:
        cur = self._conn.execute("SELECT * FROM contracts")
        return [self._row_to_contract(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Trace operations (G3)
    # ------------------------------------------------------------------

    def append_trace_entry(self, entry: TraceEntry) -> None:
        """Persist a trace entry to the trace_events table."""
        d = trace_entry_to_dict(entry)
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO trace_events (
                    id, parent_id, contract_id, event_type,
                    disclosed_assets, accepted_fragments, accepted_asset_names,
                    rejected_fragments, worker_id, candidate_raw,
                    authority_policy, authority_result, budget_remaining,
                    relation_type, relation_target, timestamp
                ) VALUES (
                    :id, :parent_id, :contract_id, :event_type,
                    :disclosed_assets, :accepted_fragments, :accepted_asset_names,
                    :rejected_fragments, :worker_id, :candidate_raw,
                    :authority_policy, :authority_result, :budget_remaining,
                    :relation_type, :relation_target, :timestamp
                )""",
                {
                    "id": d.get("id", ""),
                    "parent_id": d.get("parent_id"),
                    "contract_id": d.get("contract_id", ""),
                    "event_type": d.get("event_type", ""),
                    "disclosed_assets": json.dumps(d.get("disclosed_assets", [])),
                    "accepted_fragments": json.dumps(d.get("accepted_fragments", [])),
                    "accepted_asset_names": json.dumps(d.get("accepted_asset_names", [])),
                    "rejected_fragments": json.dumps(d.get("rejected_fragments", [])),
                    "worker_id": d.get("worker_id"),
                    "candidate_raw": d.get("candidate_raw"),
                    "authority_policy": d.get("authority_policy"),
                    "authority_result": d.get("authority_result"),
                    "budget_remaining": d.get("budget_remaining", 0),
                    "relation_type": d.get("relation_type"),
                    "relation_target": d.get("relation_target"),
                    "timestamp": d.get("timestamp", ""),
                },
            )

    def get_trace_events(self, contract_id: str | None = None) -> list[TraceEntry]:
        """Return trace entries, optionally filtered by contract_id."""
        if contract_id is not None:
            cur = self._conn.execute(
                "SELECT * FROM trace_events WHERE contract_id = ? ORDER BY rowid",
                (contract_id,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM trace_events ORDER BY rowid")
        return [self._row_to_trace_entry(row) for row in cur.fetchall()]

    def _row_to_trace_entry(self, row: sqlite3.Row) -> TraceEntry:
        """Convert a trace_events row to a TraceEntry."""
        import json as _json
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
        )

    # ------------------------------------------------------------------
    # Trace operations (040 C3)
    # ------------------------------------------------------------------

    def append_trace_entry(self, entry) -> None:
        """Insert a TraceEntry into the trace_events table."""
        from aigineering.protocol.wire import trace_entry_to_dict
        d = trace_entry_to_dict(entry)
        with self._conn:
            self._conn.execute(
                """INSERT INTO trace_events (
                    id, parent_id, contract_id, event_type,
                    disclosed_assets, accepted_fragments, accepted_asset_names,
                    rejected_fragments, worker_id, candidate_raw,
                    authority_policy, authority_result, budget_remaining,
                    relation_type, relation_target, timestamp
                ) VALUES (
                    :id, :parent_id, :contract_id, :event_type,
                    :disclosed_assets, :accepted_fragments, :accepted_asset_names,
                    :rejected_fragments, :worker_id, :candidate_raw,
                    :authority_policy, :authority_result, :budget_remaining,
                    :relation_type, :relation_target, :timestamp
                )""",
                {k: json.dumps(v) if isinstance(v, (list, dict)) else v
                 for k, v in d.items()},
            )

    def get_trace_events(self, contract_id: str | None = None) -> list:
        """Return trace events, optionally filtered by *contract_id*."""
        from aigineering.protocol.types import TraceEntry
        from aigineering.protocol.wire import trace_entry_from_dict
        if contract_id is not None:
            cur = self._conn.execute(
                "SELECT * FROM trace_events WHERE contract_id = ? ORDER BY rowid",
                (contract_id,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM trace_events ORDER BY rowid")
        return [trace_entry_from_dict(dict(row)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Claim persistence (040 C4, G8)
    # ------------------------------------------------------------------

    def persist_claim(self, claim_id: str, contract_id: str, worker_id: str,
                      lease_until: str, status: str = "active") -> None:
        """Persist a claim record to survive restarts."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO claims (id, source_asset_id, "
                "replacement_asset_id, definition_hash, claim_type, "
                "signed_by, provenance_seal, lineage_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (claim_id, contract_id, worker_id, lease_until, status, "", "", ""),
            )

    def get_claim(self, contract_id: str) -> dict | None:
        """Return the active claim for *contract_id*, or None."""
        cur = self._conn.execute(
            "SELECT id, source_asset_id, replacement_asset_id, "
            "definition_hash, claim_type FROM claims "
            "WHERE source_asset_id = ? ORDER BY rowid DESC LIMIT 1",
            (contract_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "claim_id": row["id"],
            "contract_id": row["source_asset_id"],
            "worker_id": row["replacement_asset_id"],
            "lease_until": row["definition_hash"],
            "status": row["claim_type"],
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
