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
from types import MappingProxyType
from typing import Optional

from aigineering.core.crash import check_crash_point
from aigineering.core.ids import compute_content_hash, now_iso
from aigineering.core.provenance import verify_asset_seal
from aigineering.protocol.types import Asset, Contract, TraceEntry
from aigineering.protocol.wire import (
    asset_to_dict,
    contract_to_dict,
    trace_entry_to_dict,
)

_logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 4

# ---------------------------------------------------------------------------
# Activation name extraction (shared with store.py)
# ---------------------------------------------------------------------------

_ACTIVATION_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT"})


def _extract_activation_names(expression: str) -> set[str]:
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
    signer_kind TEXT NOT NULL DEFAULT 'deterministic',
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
    origin TEXT NOT NULL DEFAULT 'human',
    minting_authority TEXT NOT NULL DEFAULT '[]',
    sensitive_input_policy TEXT
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
    timestamp TEXT NOT NULL DEFAULT '',
    usage_metadata TEXT
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

_DDL_CREATE_REPLACEMENT_CLAIMS = """
CREATE TABLE IF NOT EXISTS replacement_claims (
    claim_id TEXT PRIMARY KEY,
    source_asset_id TEXT NOT NULL,
    replacement_asset_id TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    signed_by TEXT DEFAULT '',
    provenance_seal TEXT DEFAULT ''
)
"""

_DDL_CREATE_WORKER_CLAIMS = """
CREATE TABLE IF NOT EXISTS worker_claims (
    claim_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    lease_until TEXT NOT NULL,
    status TEXT NOT NULL,
    package_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_DDL_CREATE_IDEMPOTENCY = """
CREATE TABLE IF NOT EXISTS idempotency_records (
    contract_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (contract_id, idempotency_key)
)
"""

_DDL_CREATE_ACTIVATION_REFS = """
CREATE TABLE IF NOT EXISTS contract_activation_refs (
    contract_id TEXT NOT NULL,
    asset_name TEXT NOT NULL,
    PRIMARY KEY (contract_id, asset_name)
)
"""

_DDL_CREATE_DECLARED_OUTPUTS = """
CREATE TABLE IF NOT EXISTS contract_declared_outputs (
    contract_id TEXT NOT NULL,
    output_name TEXT NOT NULL,
    PRIMARY KEY (contract_id, output_name)
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
    "CREATE INDEX IF NOT EXISTS idx_worker_claims_contract_status ON worker_claims(contract_id, status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_worker_claims_one_active ON worker_claims(contract_id) WHERE status = 'active'",
    "CREATE INDEX IF NOT EXISTS idx_idempotency_contract ON idempotency_records(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_activation_refs_asset ON contract_activation_refs(asset_name)",
    "CREATE INDEX IF NOT EXISTS idx_declared_outputs_name ON contract_declared_outputs(output_name)",
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

        self._conn.execute(_DDL_CREATE_SCHEMA_VERSION)
        self._conn.commit()

        current = self._current_schema_version()
        if current > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"SQLite schema version {current} is newer than supported "
                f"version {CURRENT_SCHEMA_VERSION}. Refusing to open — "
                f"this build cannot read a newer database."
            )

        self._create_tables()
        self._run_migrations(current)

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
            _DDL_CREATE_REPLACEMENT_CLAIMS,
            _DDL_CREATE_WORKER_CLAIMS,
            _DDL_CREATE_IDEMPOTENCY,
            _DDL_CREATE_ACTIVATION_REFS,
            _DDL_CREATE_DECLARED_OUTPUTS,
        ]:
            self._conn.execute(ddl)
        for idx_ddl in _DDL_INDEXES:
            self._conn.execute(idx_ddl)
        self._conn.commit()

    def _current_schema_version(self) -> int:
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

    def _record_schema_version(self, version: int) -> None:
        self._conn.execute("DELETE FROM schema_version")
        self._conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, now_iso()),
        )

    def _run_migrations(self, current: int) -> None:
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

    def _migrate_to_v2(self) -> None:
        """Add 040 transactional worker state and contract authority metadata."""
        self._ensure_contract_columns()
        for ddl in (_DDL_CREATE_WORKER_CLAIMS, _DDL_CREATE_IDEMPOTENCY):
            self._conn.execute(ddl)
        for idx_ddl in _DDL_INDEXES:
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
        for ddl in (_DDL_CREATE_ACTIVATION_REFS, _DDL_CREATE_DECLARED_OUTPUTS):
            self._conn.execute(ddl)
        for idx_ddl in _DDL_INDEXES:
            self._conn.execute(idx_ddl)

        # Backfill: register activation refs and declared outputs for every
        # contract already in the database.
        import json as _json

        rows = list(self._conn.execute("SELECT id, activation, outputs FROM contracts"))
        for row in rows:
            contract_id = row["id"]
            activation: str = row["activation"] or ""
            outputs_raw: str = row["outputs"] or "[]"

            for asset_name in _extract_activation_names(activation):
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

    @property
    def schema_version(self) -> int:
        return self._current_schema_version()

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
            origin=row["origin"],
            minting_authority=tuple(json.loads(row["minting_authority"] or "[]")),
            sensitive_input_policy=(
                json.loads(row["sensitive_input_policy"])
                if row["sensitive_input_policy"]
                else None
            ),
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
        with self._conn:
            self._insert_asset(asset)

    def _insert_asset(self, asset: Asset) -> None:
        d = asset_to_dict(asset)
        self._conn.execute(
            """INSERT OR REPLACE INTO assets (
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

    def add_contract(self, contract: Contract) -> None:
        d = contract_to_dict(contract)
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO contracts (
                    id, parent_id, name, description,
                    inputs, outputs, activation, budget,
                    tool_scope, labels, origin, minting_authority, sensitive_input_policy
                ) VALUES (
                    :id, :parent_id, :name, :description,
                    :inputs, :outputs, :activation, :budget,
                    :tool_scope, :labels, :origin, :minting_authority, :sensitive_input_policy
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
                    "minting_authority": json.dumps(list(d["minting_authority"])),
                    "sensitive_input_policy": (
                        json.dumps(d["sensitive_input_policy"], sort_keys=True)
                        if d["sensitive_input_policy"] is not None
                        else None
                    ),
                },
            )
            self._conn.execute(
                "DELETE FROM contract_activation_refs WHERE contract_id = ?",
                (contract.id,),
            )
            for name in _extract_activation_names(contract.activation):
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

    def get_contract(self, contract_id: str) -> Optional[Contract]:
        cur = self._conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,))
        row = cur.fetchone()
        return self._row_to_contract(row) if row else None

    def get_all_contracts(self) -> list[Contract]:
        cur = self._conn.execute("SELECT * FROM contracts")
        return [self._row_to_contract(row) for row in cur.fetchall()]

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

    # ------------------------------------------------------------------
    # Trace operations (G3, 040 C3)
    # ------------------------------------------------------------------

    def append_trace_entry(self, entry) -> None:
        """Insert a TraceEntry into the trace_events table."""
        with self._conn:
            self._insert_trace_entry(entry)

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
        d = trace_entry_to_dict(entry)
        self._conn.execute(
            """INSERT OR REPLACE INTO trace_events (
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
            if asset_id in entry.accepted_fragments
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
    ) -> None:
        """Persist a worker lease claim to survive restarts."""
        now = now_iso()
        with self._conn:
            self._conn.execute(
                """INSERT INTO worker_claims (
                    claim_id, contract_id, worker_id, lease_until, status,
                    package_id, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(claim_id) DO UPDATE SET
                    contract_id = excluded.contract_id,
                    worker_id = excluded.worker_id,
                    lease_until = excluded.lease_until,
                    status = excluded.status,
                    package_id = excluded.package_id,
                    updated_at = excluded.updated_at
                """,
                (
                    claim_id,
                    contract_id,
                    worker_id,
                    lease_until,
                    status,
                    package_id,
                    now,
                    now,
                ),
            )

    def get_claim(self, contract_id: str) -> dict | None:
        """Return the active claim for *contract_id*, or None."""
        cur = self._conn.execute(
            "SELECT claim_id, contract_id, worker_id, lease_until, status, package_id "
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
        }

    def claim_contract(
        self,
        contract_id: str,
        worker_id: str,
        lease_seconds: int = 60,
        package_id: str = "",
    ) -> dict | None:
        """Atomically claim *contract_id* for *worker_id* if no active claim exists."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claim_id = f"lease:{compute_content_hash(f'{contract_id}|{worker_id}|{now.isoformat()}')}"
        try:
            with self._conn:
                existing = self._conn.execute(
                    "SELECT claim_id, status FROM worker_claims "
                    "WHERE contract_id = ? ORDER BY rowid DESC LIMIT 1",
                    (contract_id,),
                ).fetchone()
                if existing is not None:
                    return None
                self._conn.execute(
                    """INSERT INTO worker_claims (
                        claim_id, contract_id, worker_id, lease_until, status,
                        package_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)""",
                    (
                        claim_id,
                        contract_id,
                        worker_id,
                        lease_until,
                        package_id,
                        now_iso(),
                        now_iso(),
                    ),
                )
        except sqlite3.IntegrityError:
            return None
        return self.get_claim(contract_id)

    def mark_claim_submitted(self, claim_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE worker_claims SET status = 'submitted', updated_at = ? "
                "WHERE claim_id = ?",
                (now_iso(), claim_id),
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
        self._conn.execute(
            "INSERT OR REPLACE INTO idempotency_records "
            "(contract_id, idempotency_key, result_json, created_at) VALUES (?, ?, ?, ?)",
            (
                contract_id,
                idempotency_key,
                json.dumps(result, sort_keys=True),
                now_iso(),
            ),
        )

    def commit_candidate_submission(
        self,
        accepted_assets: list[Asset],
        trace_entries: list[TraceEntry],
        idempotency_key: str,
        idempotency_result: dict,
        claim_id: str,
        worker_id: str = "",
        package_id: str = "",
    ) -> bool:
        """Commit accepted assets, trace, idempotency, and claim state atomically.

        Returns ``False`` when a claimed submission can no longer satisfy the
        active claim predicate at commit time.
        """
        with self._conn:
            for asset in accepted_assets:
                if not asset.signed_by or not verify_asset_seal(asset):
                    raise ValueError(
                        f"G3/N-P1.6: Asset '{asset.id}' rejected — missing or invalid canonical seal "
                        f"(signed_by={asset.signed_by!r})"
                    )
                self._insert_asset(asset)
            # Crash injection point: assets written, traces not yet written.
            check_crash_point("after_asset_before_trace")
            for entry in trace_entries:
                self._insert_trace_entry(entry)
            if idempotency_key:
                self.set_idempotency(
                    trace_entries[0].contract_id, idempotency_key, idempotency_result
                )
            if claim_id:
                committed_at = now_iso()
                cur = self._conn.execute(
                    "UPDATE worker_claims SET status = 'submitted', updated_at = ? "
                    "WHERE claim_id = ? AND status = 'active' "
                    "AND worker_id = ? "
                    "AND (? = '' OR package_id = ?) "
                    "AND lease_until >= ?",
                    (
                        committed_at,
                        claim_id,
                        worker_id,
                        package_id,
                        package_id,
                        committed_at,
                    ),
                )
                if cur.rowcount != 1:
                    raise sqlite3.IntegrityError(
                        "active worker claim predicate failed during submit"
                    )
        return True

    # ------------------------------------------------------------------
    # Replacement claim persistence
    # ------------------------------------------------------------------

    def add_replacement_claim(self, claim) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO claims (
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
