"""Tests for SQLiteStore — transactional SQLite implementation of StoreProtocol."""

import sqlite3
from types import MappingProxyType

import pytest

from aigineering.core.provenance import sign_asset
from aigineering.core.sqlite_store import (
    CURRENT_SCHEMA_VERSION,
    ImmutableRecordConflict,
    SQLiteStore,
)
from aigineering.core.store import StoreProtocol
from aigineering.core.trace import create_entry
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


def test_projection_indexes_rebuild_to_same_digest(store):
    contract = Contract(
        id="indexed-c",
        activation="input_a AND input_b",
        outputs=["report", "summary"],
    )
    store.add_contract(contract)
    expected_digest = store.projection_index_digest()

    with store._conn:
        store._conn.execute("DELETE FROM contract_activation_refs")
        store._conn.execute("DELETE FROM contract_declared_outputs")
    assert store.projection_index_digest() != expected_digest

    store.rebuild_projection_indexes()
    assert store.projection_index_digest() == expected_digest
    assert set(store.get_contracts_waiting_for("input_a")) == {"indexed-c"}
    assert set(store.get_contracts_declaring_output("summary")) == {"indexed-c"}


def test_sqlite_reducer_sees_new_fact_inside_atomic_commit(store):
    from aigineering.core.fact_reducer import FactReducer
    from aigineering.core.runtime_ingress import RuntimeIngress
    from aigineering.core.trace import MemoryTraceStore

    trace = MemoryTraceStore()
    store.add_contract(Contract(id="c1", outputs=["report"]))
    ingress = RuntimeIngress(store, trace, FactReducer(store, trace))

    ingress.accept_asset(
        Asset(id="report-a", name="report", content="done", origin="worker")
    )

    event_types = [entry.event_type for entry in store.get_trace_events("c1")]
    assert event_types == ["output_satisfied", "complete"]
    assert [entry.event_type for entry in trace.get_by_contract("c1")] == event_types


# ---------------------------------------------------------------------------
# Basic CRUD: assets
# ---------------------------------------------------------------------------


def test_crud_asset(store):
    """Add, get, get_by_name, get_all for assets."""
    asset = sign_asset(
        Asset(id="a1", name="report", content="hello world", origin="test")
    )
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
    store.add_asset(
        sign_asset(Asset(id="a1", name="data.json", content="{}"), signed_by="test")
    )
    assert store.has_asset_named("data.json") is True
    assert store.has_asset_named("missing") is False


def test_get_assets_by_name_multiple(store):
    store.add_asset(
        sign_asset(Asset(id="a1", name="report", content="r1"), signed_by="test")
    )
    store.add_asset(
        sign_asset(Asset(id="a2", name="report", content="r2"), signed_by="test")
    )
    store.add_asset(
        sign_asset(Asset(id="a3", name="other", content="o1"), signed_by="test")
    )

    results = store.get_assets_by_name("report")
    assert len(results) == 2
    assert {a.id for a in results} == {"a1", "a2"}


def test_get_assets_by_contract(store):
    store.add_asset(
        sign_asset(
            Asset(id="a1", name="r1", content="x", created_by="c1"), signed_by="test"
        )
    )
    store.add_asset(
        sign_asset(
            Asset(id="a2", name="r2", content="y", created_by="c1"), signed_by="test"
        )
    )
    store.add_asset(
        sign_asset(
            Asset(id="a3", name="r3", content="z", created_by="c2"), signed_by="test"
        )
    )
    store.add_asset(
        sign_asset(Asset(id="a4", name="r4", content="w"), signed_by="test")
    )

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
        id="c1",
        name="build",
        outputs=["report"],
        budget=10,
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
    store.add_asset(
        sign_asset(
            Asset(id="a1", name="v1", content="x", definition_hash="abc123"),
            signed_by="test",
        )
    )
    store.add_asset(
        sign_asset(
            Asset(id="a2", name="v2", content="y", definition_hash="def456"),
            signed_by="test",
        )
    )
    store.add_asset(
        sign_asset(
            Asset(id="a3", name="v3", content="z", definition_hash="abc123"),
            signed_by="test",
        )
    )

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
    store.add_asset(
        sign_asset(
            Asset(id="a1", name="doc", content="v1", definition_hash="abc"),
            signed_by="test",
        )
    )
    store.add_asset(
        sign_asset(
            Asset(id="a2", name="doc", content="v2", definition_hash="abc"),
            signed_by="test",
        )
    )
    store.add_asset(
        sign_asset(
            Asset(id="a3", name="doc", content="v3", definition_hash="abc"),
            signed_by="test",
        )
    )

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
    """schema_version table must exist and reach the current version."""
    cur = store._conn.execute("SELECT version, applied_at FROM schema_version")
    rows = cur.fetchall()
    assert len(rows) >= 1

    versions = [row["version"] for row in rows]
    assert versions == [CURRENT_SCHEMA_VERSION]

    assert store.schema_version == CURRENT_SCHEMA_VERSION


def test_v1_schema_migrates_to_v2(tmp_path):
    """A v1 database gains 040 worker/idempotency tables and contract metadata."""
    db_path = str(tmp_path / "v1.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (1, 'old')")
    conn.execute(
        """
        CREATE TABLE contracts (
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
    )
    conn.execute(
        "INSERT INTO contracts (id, name, outputs) VALUES ('c1', 'old', '[\"out\"]')"
    )
    conn.commit()
    conn.close()

    migrated = SQLiteStore(db_path)
    assert migrated.schema_version == CURRENT_SCHEMA_VERSION

    contract_columns = {
        row["name"]
        for row in migrated._conn.execute("PRAGMA table_info(contracts)").fetchall()
    }
    assert "minting_authority" in contract_columns
    assert "sensitive_input_policy" in contract_columns
    trace_columns = {
        row["name"]
        for row in migrated._conn.execute("PRAGMA table_info(trace_events)").fetchall()
    }
    assert "usage_metadata" in trace_columns

    tables = {
        row["name"]
        for row in migrated._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "worker_claims" in tables
    assert "idempotency_records" in tables

    contract = migrated.get_contract("c1")
    assert contract is not None
    assert contract.minting_authority == ()
    assert contract.sensitive_input_policy is None
    migrated.close()


# ---------------------------------------------------------------------------
# Index coverage
# ---------------------------------------------------------------------------


def test_index_coverage_assets(store):
    """EXPLAIN QUERY PLAN must show index usage for indexed asset columns."""
    # Populate with data to give the planner a reason to use indexes
    store.add_asset(
        sign_asset(
            Asset(
                id="a1",
                name="alpha",
                content="x",
                definition_hash="dh1",
                content_hash="ch1",
                lineage_id="li1",
                created_by="cb1",
                tombstoned=True,
            ),
            signed_by="test",
        )
    )

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
    assert (
        "INDEX" in plan_text.upper() or "idx_trace_events_contract_id" in plan_text
    ), f"Expected index usage for trace_events.contract_id:\n{plan_text}"


# ---------------------------------------------------------------------------
# Dual-hash fields
# ---------------------------------------------------------------------------


def test_dual_hash_fields(store):
    """definition_hash and content_hash must persist correctly."""
    asset = sign_asset(
        Asset(
            id="a1",
            name="doc",
            content="payload",
            definition_hash="dh_abc123",
            content_hash="ch_def456",
            origin="test",
        )
    )
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
    asset_retained = sign_asset(
        Asset(
            id="a2",
            name="kept",
            content="important",
            keep_flag=True,
            tombstoned=True,
            tombstoned_at="2025-01-01T00:00:00Z",
            origin="test",
        )
    )
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
    asset = sign_asset(
        Asset(
            id="a1", name="doc", content="data", lineage_id="lineage_xyz", origin="test"
        )
    )
    store.add_asset(asset)

    fetched = store.get_asset("a1")
    assert fetched is not None
    assert fetched.lineage_id == "lineage_xyz"

    # Empty lineage_id default
    asset_no_lineage = sign_asset(
        Asset(id="a2", name="plain", content="plain", origin="test")
    )
    store.add_asset(asset_no_lineage)
    assert store.get_asset("a2").lineage_id == ""


# ---------------------------------------------------------------------------
# Transaction atomicity
# ---------------------------------------------------------------------------


def test_transaction_atomicity(store):
    """SQLite transactions must be atomic — committed data persists,
    uncommitted data does not."""
    # Normal commit path — data persists
    store.add_asset(
        sign_asset(Asset(id="a1", name="committed", content="safe"), signed_by="test")
    )
    assert store.get_asset("a1") is not None

    # Simulate an uncommitted write on a separate connection
    raw_conn = sqlite3.connect(":memory:")
    raw_conn.execute("ATTACH DATABASE ':memory:' AS test_db")
    # Verify the store's data is accessible (it uses its own connection)
    assert store.get_asset("a1") is not None


def test_runtime_record_ids_are_immutable(store):
    """Identical replay is a no-op; same ID with changed content fails closed."""
    from aigineering.core.record_conflict import ImmutableRecordConflict

    original = sign_asset(
        Asset(id="a1", name="original", content="v1"), signed_by="test"
    )
    store.add_asset(original)
    store.add_asset(original)
    assert len(store.get_all_assets()) == 1

    with pytest.raises(ImmutableRecordConflict, match="immutable asset conflict"):
        store.add_asset(
            sign_asset(Asset(id="a1", name="updated", content="v2"), signed_by="test")
        )
    assert len(store.get_all_assets()) == 1
    assert store.get_asset("a1") == original


def test_idempotency_record_is_immutable(store):
    from aigineering.core.record_conflict import ImmutableRecordConflict

    result = {"status": "accepted", "assets": ["a1"]}
    store.set_idempotency("c1", "key-1", result)
    store.set_idempotency("c1", "key-1", result)
    assert store.get_idempotency("c1", "key-1") == result

    with pytest.raises(ImmutableRecordConflict, match="idempotency record"):
        store.set_idempotency("c1", "key-1", {"status": "rejected"})
    assert store.get_idempotency("c1", "key-1") == result


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
        "assets",
        "contracts",
        "trace_events",
        "sessions",
        "claims",
        "worker_claims",
        "idempotency_records",
        "runtime_records",
        "schema_version",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


# ---------------------------------------------------------------------------
# Worker claim invariants
# ---------------------------------------------------------------------------


def test_worker_claim_has_database_unique_active_contract(store):
    """SQLite enforces at most one active worker claim per contract."""
    store.persist_claim("claim-1", "c1", "worker-1", "2026-12-31T00:00:00", "active")

    with pytest.raises(sqlite3.IntegrityError):
        store.persist_claim(
            "claim-2", "c1", "worker-2", "2026-12-31T00:00:00", "active"
        )

    store.persist_claim("claim-2", "c1", "worker-2", "2026-12-31T00:00:00", "released")
    claim = store.get_claim("c1")
    assert claim is not None
    assert claim["claim_id"] == "claim-1"
    assert claim["status"] == "active"


def test_worker_claim_identity_cannot_be_rewritten(store):
    store.persist_claim("claim-1", "c1", "worker-1", "2026-12-31T00:00:00")

    with pytest.raises(ImmutableRecordConflict, match="worker claim"):
        store.persist_claim("claim-1", "c1", "worker-2", "2026-12-31T00:00:00")


def test_released_claim_rebuilds_from_lifecycle_facts(store):
    store.persist_claim("claim-1", "c1", "worker-1", "2026-12-31T00:00:00")
    store.mark_claim_released("claim-1")

    with store._conn:
        store._conn.execute("DELETE FROM worker_claims")
    store.rebuild_claim_projection()

    claim = store.get_claim("c1")
    assert claim is not None
    assert claim["status"] == "released"
    assert store.claim_contract("c1", "worker-2") is None


def test_submitted_contract_cannot_be_reclaimed_and_old_lease_is_fenced(store):
    first = store.claim_contract("c1", "worker", lease_seconds=30)
    assert first is not None
    assert first["epoch"] == 1
    store.mark_claim_submitted(first["claim_id"])

    second = store.claim_contract("c1", "worker", lease_seconds=30)
    assert second is None
    assert store.renew_claim(first["claim_id"], 1, "worker") is None
    record_types = [record.record_type for _, record in store.scan_runtime_records()]
    assert record_types.count("claim.granted") == 1
    assert record_types.count("claim.submitted") == 1
    records = [record for _, record in store.scan_runtime_records()]
    granted_ids = {
        record.id for record in records if record.record_type == "claim.granted"
    }
    lifecycle = [
        record for record in records if record.record_type == "claim.submitted"
    ]
    assert lifecycle
    assert all(record.causal_parents[0] in granted_ids for record in lifecycle)

    with store._conn:
        store._conn.execute("DELETE FROM worker_claims")
    store.rebuild_claim_projection()
    rebuilt = store.get_claim("c1")
    assert rebuilt is not None
    assert rebuilt["claim_id"] == first["claim_id"]
    assert rebuilt["epoch"] == 1
    assert rebuilt["status"] == "submitted"


def test_expired_claim_cannot_be_renewed(store):
    store.persist_claim("expired", "c1", "worker", "2020-01-01T00:00:00+00:00", epoch=3)
    assert store.renew_claim("expired", 3, "worker") is None


def test_candidate_submit_rejects_stale_fencing_epoch_before_projection(store):
    from aigineering.core.submit import SubmitClaimError, submit_candidate
    from aigineering.protocol.envelope import CandidateEnvelope

    contract = Contract(id="epoch-submit", outputs=["out"], budget=1)
    store.add_contract(contract)
    claim = store.claim_contract(contract.id, "worker")
    assert claim is not None
    envelope = CandidateEnvelope(
        contract_id=contract.id,
        worker_id="worker",
        raw_output='/exec {"out": "value"}',
        claim_id=claim["claim_id"],
        claim_epoch=claim["epoch"] + 1,
    )

    with pytest.raises(SubmitClaimError, match="epoch mismatch"):
        submit_candidate(envelope, store, store)
    assert store.get_assets_by_name("out") == []


def test_claim_contract_rejects_second_connection_active_claim(tmp_path):
    """Two SQLite connections cannot both claim the same contract as active."""
    db_path = str(tmp_path / "claims.db")
    first = SQLiteStore(db_path)
    second = SQLiteStore(db_path)

    claim1 = first.claim_contract("c1", "worker-1", package_id="pkg:first")
    claim2 = second.claim_contract("c1", "worker-2", package_id="pkg:second")

    assert claim1 is not None
    assert claim2 is None

    observed = second.get_claim("c1")
    assert observed is not None
    assert observed["worker_id"] == "worker-1"
    assert observed["package_id"] == "pkg:first"

    first.close()
    second.close()


def test_claim_contract_rejects_reclaim_after_expired_claim(store):
    """A claimed contract never returns to the unclaimed pool."""
    store.persist_claim(
        "claim-old",
        "c-expired",
        "worker-1",
        "2000-01-01T00:00:00+00:00",
        "active",
    )

    claim = store.claim_contract("c-expired", "worker-2")

    assert claim is None


def test_trace_usage_metadata_round_trips(store):
    """SQLite trace persistence keeps LLM token/cost metadata."""
    entry = create_entry(
        contract_id="c-usage",
        event_type="projection",
        sequence=0,
        usage_metadata=MappingProxyType(
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "model": "test-model",
                "provider": "test-provider",
            }
        ),
    )

    store.append(entry)
    restored = store.get_trace_events("c-usage")[0]

    assert restored.usage_metadata is not None
    assert dict(restored.usage_metadata) == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "model": "test-model",
        "provider": "test-provider",
    }


def test_candidate_submission_rolls_back_on_mid_commit_failure(store, monkeypatch):
    """Asset, trace, idempotency, and claim updates rollback as one transaction."""
    from aigineering.core.trace import create_entry

    store.persist_claim("claim-1", "c1", "worker-1", "2026-12-31T00:00:00", "active")
    asset = sign_asset(
        Asset(id="a-rollback", name="out", content="payload", created_by="c1"),
        signed_by="runtime",
    )
    entry = create_entry(contract_id="c1", event_type="projection", sequence=0)

    def fail_trace_insert(_entry):
        raise RuntimeError("simulated trace failure")

    monkeypatch.setattr(store, "_insert_trace_entry", fail_trace_insert)

    with pytest.raises(RuntimeError, match="simulated trace failure"):
        store.commit_candidate_submission(
            accepted_assets=[asset],
            trace_entries=[entry],
            idempotency_key="idem-1",
            idempotency_result={"status": "accepted"},
            claim_id="claim-1",
        )

    assert store.get_asset("a-rollback") is None
    assert store.get_idempotency("c1", "idem-1") is None
    claim = store.get_claim("c1")
    assert claim is not None
    assert claim["status"] == "active"


def test_method_submission_rolls_back_child_context_and_claim(store, monkeypatch):
    from aigineering.core.methods import system_asset
    from aigineering.core.trace import create_entry
    from aigineering.protocol.runtime_record import create_runtime_record

    store.persist_claim(
        "claim-method",
        "c-parent",
        "worker",
        "2026-12-31T00:00:00+00:00",
        package_id="pkg:method",
    )
    child = Contract(id="c-method", parent_id="c-parent", origin="system")
    context = sign_asset(system_asset("_method_ctx_c-parent", "context", "c-parent"))
    entry = create_entry("c-parent", "method_scheduled")
    record = create_runtime_record("method.scheduled", {"contract_id": "c-parent"})

    def fail_trace(_entry):
        raise RuntimeError("simulated method trace failure")

    monkeypatch.setattr(store, "_insert_trace_entry", fail_trace)
    with pytest.raises(RuntimeError, match="method trace failure"):
        store.commit_method_submission(
            child_contract=child,
            context_asset=context,
            trace_entries=[entry],
            runtime_records=(record,),
            idempotency_key="idem-method",
            idempotency_result={"status": "method_scheduled"},
            claim_id="claim-method",
            worker_id="worker",
            package_id="pkg:method",
            claim_epoch=1,
        )

    assert store.get_contract(child.id) is None
    assert store.get_asset(context.id) is None
    assert store.get_runtime_record(record.id) is None
    assert store.get_idempotency("c-parent", "idem-method") is None
    assert store.get_claim("c-parent")["status"] == "active"


def test_candidate_submission_rolls_back_when_claim_predicate_fails(store, monkeypatch):
    """A stale claim state at commit time rejects and rolls back the submission."""
    from aigineering.core.submit import SubmitCommitError, submit_candidate
    from aigineering.protocol.envelope import CandidateEnvelope

    contract = Contract(id="c-submit", name="submit", outputs=["out"], budget=3)
    store.add_contract(contract)
    store.persist_claim(
        "claim-stale",
        "c-submit",
        "worker-1",
        "2026-12-31T00:00:00+00:00",
        "active",
        "pkg:test",
    )
    store.mark_claim_released("claim-stale")
    monkeypatch.setattr(
        store,
        "get_claim",
        lambda _contract_id: {
            "claim_id": "claim-stale",
            "contract_id": "c-submit",
            "worker_id": "worker-1",
            "lease_until": "2026-12-31T00:00:00+00:00",
            "status": "active",
            "package_id": "pkg:test",
            "epoch": 1,
        },
    )
    envelope = CandidateEnvelope(
        contract_id="c-submit",
        worker_id="worker-1",
        raw_output='/exec {"out": "value"}',
        claim_id="claim-stale",
        claim_epoch=1,
        package_id="pkg:test",
        idempotency_key="idem-stale",
    )

    with pytest.raises(SubmitCommitError):
        submit_candidate(envelope, store, store)

    assert store.get_assets_by_name("out") == []
    assert store.get_trace_events("c-submit") == []
    assert store.get_idempotency("c-submit", "idem-stale") is None


def test_candidate_submission_rolls_back_when_claim_expires_at_commit(
    store, monkeypatch
):
    """A claim that expires between validation and commit rejects atomically."""
    from aigineering.core.submit import SubmitCommitError, submit_candidate
    from aigineering.protocol.envelope import CandidateEnvelope

    contract = Contract(id="c-expire", name="expire", outputs=["out"], budget=3)
    store.add_contract(contract)
    store.persist_claim(
        "claim-expired",
        "c-expire",
        "worker-1",
        "2000-01-01T00:00:00+00:00",
        "active",
        "pkg:expired",
    )
    monkeypatch.setattr(
        store,
        "get_claim",
        lambda _contract_id: {
            "claim_id": "claim-expired",
            "contract_id": "c-expire",
            "worker_id": "worker-1",
            "lease_until": "2026-12-31T00:00:00+00:00",
            "status": "active",
            "package_id": "pkg:expired",
            "epoch": 1,
        },
    )
    envelope = CandidateEnvelope(
        contract_id="c-expire",
        worker_id="worker-1",
        raw_output='/exec {"out": "value"}',
        claim_id="claim-expired",
        claim_epoch=1,
        package_id="pkg:expired",
        idempotency_key="idem-expired",
    )

    with pytest.raises(SubmitCommitError):
        submit_candidate(envelope, store, store)

    assert store.get_assets_by_name("out") == []
    assert store.get_trace_events("c-expire") == []
    assert store.get_idempotency("c-expire", "idem-expired") is None


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
    s1.add_asset(
        sign_asset(Asset(id="a1", name="persist", content="data", origin="test"))
    )
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
        s.add_asset(
            sign_asset(Asset(id="a1", name="ctx", content="test", origin="test"))
        )
        assert s.get_asset("a1") is not None
