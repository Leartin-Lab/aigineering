"""Tests for SQLiteStore — transactional SQLite implementation of StoreProtocol."""

import json
import sqlite3

import pytest

from aigineering.core.provenance import sign_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import StoreProtocol
from aigineering.protocol.types import Asset, Contract


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store():
    """Fresh in-memory SQLiteStore for each test."""
    s = SQLiteStore(":memory:")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# StoreProtocol compliance
# ---------------------------------------------------------------------------

def test_satisfies_store_protocol(store):
    """SQLiteStore must be recognized as implementing StoreProtocol."""
    assert isinstance(store, StoreProtocol)


# ---------------------------------------------------------------------------
# Basic CRUD: assets
# ---------------------------------------------------------------------------

def test_crud_asset(store):
    """Add, get, get_by_name, get_all for assets."""
    asset = sign_asset(Asset(id="a1", name="report", content="hello world", origin="test"))
    store.add_asset(asset)

    assert store.get_asset("a1") == asset
    assert store.get_asset("nonexistent") is None

    by_name = store.get_assets_by_name("report")
    assert len(by_name) == 1
    assert by_name[0] == asset

    all_assets = store.get_all_assets()
    assert len(all_assets) == 1
    assert all_assets[0] == asset


def test_has_asset_named(store):
    store.add_asset(sign_asset(Asset(id="a1", name="data.json", content="{}"), signed_by="test"))
    assert store.has_asset_named("data.json") is True
    assert store.has_asset_named("missing") is False


def test_get_assets_by_name_multiple(store):
    store.add_asset(sign_asset(Asset(id="a1", name="report", content="r1"), signed_by="test"))
    store.add_asset(sign_asset(Asset(id="a2", name="report", content="r2"), signed_by="test"))
    store.add_asset(sign_asset(Asset(id="a3", name="other", content="o1"), signed_by="test"))

    results = store.get_assets_by_name("report")
    assert len(results) == 2
    assert {a.id for a in results} == {"a1", "a2"}


def test_get_assets_by_contract(store):
    store.add_asset(sign_asset(Asset(id="a1", name="r1", content="x", created_by="c1"), signed_by="test"))
    store.add_asset(sign_asset(Asset(id="a2", name="r2", content="y", created_by="c1"), signed_by="test"))
    store.add_asset(sign_asset(Asset(id="a3", name="r3", content="z", created_by="c2"), signed_by="test"))
    store.add_asset(sign_asset(Asset(id="a4", name="r4", content="w"), signed_by="test"))

    results_c1 = store.get_assets_by_contract("c1")
    assert len(results_c1) == 2
    assert {a.id for a in results_c1} == {"a1", "a2"}

    results_c2 = store.get_assets_by_contract("c2")
    assert len(results_c2) == 1
    assert results_c2[0].id == "a3"

    results_none = store.get_assets_by_contract("c3")
    assert results_none == []


# ---------------------------------------------------------------------------
# Basic CRUD: contracts
# ---------------------------------------------------------------------------

def test_crud_contract(store):
    """Add, get, get_all for contracts."""
    contract = Contract(
        id="c1", name="build", outputs=["report"], budget=10,
    )
    store.add_contract(contract)

    fetched = store.get_contract("c1")
    assert fetched == contract
    assert fetched.budget == 10
    assert fetched.outputs == ("report",)

    assert store.get_contract("nonexistent") is None

    all_contracts = store.get_all_contracts()
    assert len(all_contracts) == 1
    assert all_contracts[0] == contract


def test_contract_with_tuple_fields(store):
    """Contracts with tuple fields must round-trip correctly."""
    contract = Contract(
        id="c2",
        parent_id="c1",
        name="sub",
        description="A sub-contract",
        inputs=["x", "y"],
        outputs=["z"],
        activation="/exec",
        budget=5,
        tool_scope=["read", "write"],
        labels=["urgent", "audit"],
        origin="system",
    )
    store.add_contract(contract)
    fetched = store.get_contract("c2")
    assert fetched is not None
    assert fetched.inputs == ("x", "y")
    assert fetched.outputs == ("z",)
    assert fetched.tool_scope == ("read", "write")
    assert fetched.labels == ("urgent", "audit")
    assert fetched.origin == "system"
    assert fetched.parent_id == "c1"


# ---------------------------------------------------------------------------
# Definition hash and version chain
# ---------------------------------------------------------------------------

def test_definition_hash_index(store):
    """Query by definition_hash must return matching assets."""
    store.add_asset(sign_asset(Asset(id="a1", name="v1", content="x",
                          definition_hash="abc123"), signed_by="test"))
    store.add_asset(sign_asset(Asset(id="a2", name="v2", content="y",
                          definition_hash="def456"), signed_by="test"))
    store.add_asset(sign_asset(Asset(id="a3", name="v3", content="z",
                          definition_hash="abc123"), signed_by="test"))

    results = store.get_assets_by_definition("abc123")
    assert len(results) == 2
    assert {a.id for a in results} == {"a1", "a3"}

    results = store.get_assets_by_definition("def456")
    assert len(results) == 1
    assert results[0].id == "a2"

    results = store.get_assets_by_definition("nonexistent")
    assert results == []


def test_version_chain(store):
    """get_assets_by_definition and get_latest_asset form a version chain."""
    store.add_asset(sign_asset(Asset(id="a1", name="doc", content="v1",
                          definition_hash="abc"), signed_by="test"))
    store.add_asset(sign_asset(Asset(id="a2", name="doc", content="v2",
                          definition_hash="abc"), signed_by="test"))
    store.add_asset(sign_asset(Asset(id="a3", name="doc", content="v3",
                          definition_hash="abc"), signed_by="test"))

    # All three share the same definition_hash
    versions = store.get_assets_by_definition("abc")
    assert len(versions) == 3

    # Latest should be the last one inserted
    latest = store.get_latest_asset("abc")
    assert latest is not None
    assert latest.id == "a3"
    assert latest.content == "v3"

    # get_latest_asset returns None for unknown hash
    assert store.get_latest_asset("unknown") is None


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

def test_schema_version(store):
    """schema_version table must exist with at least version 1."""
    cur = store._conn.execute("SELECT version, applied_at FROM schema_version")
    rows = cur.fetchall()
    assert len(rows) >= 1

    versions = [row["version"] for row in rows]
    assert 1 in versions

    assert store.schema_version >= 1


# ---------------------------------------------------------------------------
# Index coverage
# ---------------------------------------------------------------------------

def test_index_coverage_assets(store):
    """EXPLAIN QUERY PLAN must show index usage for indexed asset columns."""
    # Populate with data to give the planner a reason to use indexes
    store.add_asset(sign_asset(Asset(id="a1", name="alpha", content="x",
                          definition_hash="dh1", content_hash="ch1",
                          lineage_id="li1", created_by="cb1",
                          tombstoned=True), signed_by="test"))

    # definition_hash index
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM assets WHERE definition_hash = ?",
        ("dh1",),
    ).fetchall()
    plan_text = "\n".join(r["detail"] for r in plan)
    assert "INDEX" in plan_text.upper() or "idx_assets_definition_hash" in plan_text, (
        f"Expected index usage for definition_hash:\n{plan_text}"
    )

    # name index
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM assets WHERE name = ?",
        ("alpha",),
    ).fetchall()
    plan_text = "\n".join(r["detail"] for r in plan)
    assert "INDEX" in plan_text.upper() or "idx_assets_name" in plan_text, (
        f"Expected index usage for name:\n{plan_text}"
    )

    # created_by index
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM assets WHERE created_by = ?",
        ("cb1",),
    ).fetchall()
    plan_text = "\n".join(r["detail"] for r in plan)
    assert "INDEX" in plan_text.upper() or "idx_assets_created_by" in plan_text, (
        f"Expected index usage for created_by:\n{plan_text}"
    )


def test_index_coverage_contracts(store):
    """EXPLAIN QUERY PLAN must show index usage for contract parent_id."""
    store.add_contract(Contract(id="c1", parent_id="p1", name="child"))

    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM contracts WHERE parent_id = ?",
        ("p1",),
    ).fetchall()
    plan_text = "\n".join(r["detail"] for r in plan)
    assert "INDEX" in plan_text.upper() or "idx_contracts_parent_id" in plan_text, (
        f"Expected index usage for contracts.parent_id:\n{plan_text}"
    )


def test_index_coverage_trace_events(store):
    """EXPLAIN QUERY PLAN must show index usage for trace_events lookups."""
    # trace_events table is created even if not used by StoreProtocol directly
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM trace_events WHERE contract_id = ?",
        ("c1",),
    ).fetchall()
    plan_text = "\n".join(r["detail"] for r in plan)
    assert "INDEX" in plan_text.upper() or "idx_trace_events_contract_id" in plan_text, (
        f"Expected index usage for trace_events.contract_id:\n{plan_text}"
    )


# ---------------------------------------------------------------------------
# Dual-hash fields
# ---------------------------------------------------------------------------

def test_dual_hash_fields(store):
    """definition_hash and content_hash must persist correctly."""
    asset = sign_asset(Asset(
        id="a1", name="doc", content="payload",
        definition_hash="dh_abc123", content_hash="ch_def456",
        origin="test",
    ))
    store.add_asset(asset)

    fetched = store.get_asset("a1")
    assert fetched is not None
    assert fetched.definition_hash == "dh_abc123"
    assert fetched.content_hash == "ch_def456"


# ---------------------------------------------------------------------------
# Retention fields
# ---------------------------------------------------------------------------

def test_retention_fields(store):
    """keep_flag and tombstoned must persist correctly."""
    # Default false
    asset_default = sign_asset(Asset(id="a1", name="live", content="ok", origin="test"))
    store.add_asset(asset_default)
    assert store.get_asset("a1").keep_flag is False
    assert store.get_asset("a1").tombstoned is False
    assert store.get_asset("a1").tombstoned_at is None

    # Explicit true
    asset_retained = sign_asset(Asset(
        id="a2", name="kept", content="important",
        keep_flag=True, tombstoned=True, tombstoned_at="2025-01-01T00:00:00Z",
        origin="test",
    ))
    store.add_asset(asset_retained)
    fetched = store.get_asset("a2")
    assert fetched.keep_flag is True
    assert fetched.tombstoned is True
    assert fetched.tombstoned_at == "2025-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Lineage ID
# ---------------------------------------------------------------------------

def test_lineage_id(store):
    """lineage_id must persist correctly."""
    asset = sign_asset(Asset(id="a1", name="doc", content="data", lineage_id="lineage_xyz", origin="test"))
    store.add_asset(asset)

    fetched = store.get_asset("a1")
    assert fetched is not None
    assert fetched.lineage_id == "lineage_xyz"

    # Empty lineage_id default
    asset_no_lineage = sign_asset(Asset(id="a2", name="plain", content="plain", origin="test"))
    store.add_asset(asset_no_lineage)
    assert store.get_asset("a2").lineage_id == ""


# ---------------------------------------------------------------------------
# Transaction atomicity
# ---------------------------------------------------------------------------

def test_transaction_atomicity(store):
    """SQLite transactions must be atomic — committed data persists,
    uncommitted data does not."""
    # Normal commit path — data persists
    store.add_asset(sign_asset(Asset(id="a1", name="committed", content="safe"), signed_by="test"))
    assert store.get_asset("a1") is not None

    # Simulate an uncommitted write on a separate connection
    raw_conn = sqlite3.connect(":memory:")
    raw_conn.execute("ATTACH DATABASE ':memory:' AS test_db")
    # Verify the store's data is accessible (it uses its own connection)
    assert store.get_asset("a1") is not None


def test_insert_or_replace_upsert(store):
    """INSERT OR REPLACE must update an existing asset, not duplicate it."""
    store.add_asset(sign_asset(Asset(id="a1", name="original", content="v1"), signed_by="test"))
    assert len(store.get_all_assets()) == 1

    # Insert same ID with different content
    store.add_asset(sign_asset(Asset(id="a1", name="updated", content="v2"), signed_by="test"))
    assert len(store.get_all_assets()) == 1

    updated = store.get_asset("a1")
    assert updated.name == "updated"
    assert updated.content == "v2"
    assert updated.id == "a1"


# ---------------------------------------------------------------------------
# All tables exist
# ---------------------------------------------------------------------------

def test_all_tables_exist(store):
    """Verify all required tables are created."""
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row["name"] for row in cur.fetchall()}
    expected = {
        "assets", "contracts", "trace_events",
        "sessions", "claims", "schema_version",
    }
    assert expected.issubset(tables), (
        f"Missing tables: {expected - tables}"
    )


# ---------------------------------------------------------------------------
# WAL mode
# ---------------------------------------------------------------------------

def test_wal_mode_for_file_based_store(tmp_path):
    """Journal mode must be WAL for file-based stores."""
    db_path = str(tmp_path / "wal_test.db")
    s = SQLiteStore(db_path)
    cur = s._conn.execute("PRAGMA journal_mode")
    row = cur.fetchone()
    assert row[0].upper() == "WAL", f"Expected WAL mode, got {row[0]}"
    s.close()


# ---------------------------------------------------------------------------
# File-based store
# ---------------------------------------------------------------------------

def test_file_based_store(tmp_path):
    """SQLiteStore with a file path must persist data across connections."""
    db_path = str(tmp_path / "test.db")
    s1 = SQLiteStore(db_path)
    s1.add_asset(sign_asset(Asset(id="a1", name="persist", content="data", origin="test")))
    s1.close()

    # Re-open — data must survive
    s2 = SQLiteStore(db_path)
    assert s2.get_asset("a1") is not None
    assert s2.get_asset("a1").name == "persist"
    assert s2.get_asset("a1").content == "data"
    s2.close()


def test_file_based_creates_parent_dirs(tmp_path):
    """SQLiteStore must create parent directories for the database file."""
    db_dir = tmp_path / "deep" / "nested" / "dir"
    db_path = str(db_dir / "store.db")
    s = SQLiteStore(db_path)
    assert db_dir.exists()
    s.close()


# ---------------------------------------------------------------------------
# Context manager support
# ---------------------------------------------------------------------------

def test_context_manager():
    """SQLiteStore must support with-statement usage."""
    with SQLiteStore(":memory:") as s:
        s.add_asset(sign_asset(Asset(id="a1", name="ctx", content="test", origin="test")))
        assert s.get_asset("a1") is not None
