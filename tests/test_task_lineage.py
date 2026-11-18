"""Read-only lineage over signed ordinary task publications."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli import task_lineage as lineage_cli
from aigineering.cli.task import task_group
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import contract_from_fields
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain
from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
    candidate_received_record,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.wire import contract_from_dict
from aigineering.task_lineage import _cycle_members, task_lineage
from aigineering.task_publication import publish_task_contract, retry_local_task_result
from test_redis_collaboration import MemoryRedis

_ROOT = Path(__file__).resolve().parents[1]


def _historical_handoff_fixture(tmp_path):
    """Trust-import immutable signed vector members without running a projector."""
    document = json.loads(
        (_ROOT / "conformance/v0.5.13/task-handoff-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    vector = document["effect_vectors"][0]
    genesis = genesis_manifest_from_dict(document["genesis"])
    signer = Ed25519Signer.from_private_key_hex(
        document["ed25519_test_key"]["private_key_hex"]
    )
    actor = genesis.root_keys[0]
    store = SQLiteStore(tmp_path / "historical-handoff.sqlite")
    initialize_genesis(store, genesis)

    source = contract_from_dict(document["contract"]["value"])
    publisher = CandidatePublisher(store, store, genesis, actor, signer)
    source_decision = publisher.publish(
        (contract_declaration_effect(source),),
        idempotency_key="lineage-historical-source",
    )
    assert source_decision.accepted

    candidate = candidate_proposal_from_dict(vector["candidate"]["value"])
    verify_candidate_proposal(candidate, genesis)
    receipt = candidate_received_record(candidate, genesis)
    members = tuple(
        create_runtime_record(
            item["record_type"],
            item["payload"],
            causal_parents=item["causal_parents"],
            schema_version=item.get("schema_version", 1),
        )
        for item in vector["records"]
    )
    assert all(
        record.id == item["id"] for record, item in zip(members, vector["records"])
    )
    assert receipt.id in {
        parent for member in members for parent in member.causal_parents
    }
    from aigineering.core.publication_history import candidate_commit_head

    head = candidate_commit_head(receipt, members)
    target = contract_from_dict(vector["successor"])
    with store._conn:
        store._insert_contract(target)
        for record in (receipt, *members, head):
            store._insert_runtime_record(record)
    return store, source, target


def _retry_world(tmp_path, monkeypatch):
    import aigineering.application as application
    import aigineering.cli.infrastructure as infrastructure
    import aigineering.cli.retry as retry_cli
    import aigineering.cli.task_board as task_board
    import aigineering.local_publication as local_publication

    db = tmp_path / ".aig" / "store.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )
    redis = MemoryRedis()
    for module in (
        application,
        infrastructure,
        retry_cli,
        task_board,
        local_publication,
    ):
        if hasattr(module, "require_redis"):
            monkeypatch.setattr(module, "require_redis", lambda *_a, **_kw: redis)
    monkeypatch.setattr(local_publication, "sync_runtime", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        task_board,
        "collaboration_service",
        lambda _s: type("Sync", (), {"sync": lambda self: None})(),
    )
    monkeypatch.setattr(task_board, "persistent_store", lambda: SQLiteStore(db))
    monkeypatch.setattr(retry_cli, "_persistent_store", lambda: SQLiteStore(db))

    store = SQLiteStore(db)
    ensure_local_domain(store)
    funder = publish_task_contract(
        store,
        build_control_plane_contract(
            name="lineage funder", description="ordinary parent", outputs=(), budget=8
        ),
    )
    source = contract_from_fields(
        name="lineage source",
        description="PRIVATE: ordinary signed retry source",
        inputs=(),
        outputs=("answer",),
        activation="",
        budget=3,
        parent_id=funder.id,
        tool_scope=(),
        labels=(),
        origin="human",
    )
    source = publish_task_contract(store, source)
    return store, db, source


def _recovery_world(tmp_path):
    """Build one real failing tool task and its normally funded recovery."""
    from test_tool_recovery_funding import _fail_once, _world

    env = _world(tmp_path)
    _claim, failure = _fail_once(env, env.source, "lineage-tool-failure")
    recovery = next(
        task
        for task in env.store.get_all_contracts()
        if task.parent_id == env.consumer.id
        and task.id != env.source.id
        and task.outputs == env.source.outputs
    )
    return env, failure, recovery


def test_retry_lineage_is_signed_bidirectional_and_read_only(tmp_path, monkeypatch):
    store, db, source = _retry_world(tmp_path, monkeypatch)
    try:
        first_result, _ = retry_local_task_result(store, source.id)
        first = store.get_contract(first_result["retry_contract_id"])
        before_records = tuple(store.scan_runtime_records())
        before_revision = store.get_runtime_revision()

        left = task_lineage(store, source.id)["data"]
        right = task_lineage(store, first.id)["data"]
        assert left["contract_ids"] == right["contract_ids"]
        assert left["edges"] == right["edges"]
        retry_edges = [
            edge for edge in left["edges"] if edge["kind"] == "retry_publication"
        ]
        assert {
            (e["source_contract_id"], e["target_contract_id"]) for e in retry_edges
        } == {(source.id, first.id)}
        for edge in retry_edges:
            assert all(
                edge[key]
                for key in (
                    "candidate_id",
                    "source_receipt_id",
                    "source_head_id",
                    "target_receipt_id",
                    "target_head_id",
                )
            )
        assert store.get_runtime_revision() == before_revision
        assert tuple(store.scan_runtime_records()) == before_records

        reopened = SQLiteStore(db)
        try:
            reopened_view = task_lineage(reopened, first.id)["data"]
            assert reopened_view["edges"] == left["edges"]
            assert tuple(reopened.scan_runtime_records()) == before_records
        finally:
            reopened.close()
    finally:
        store.close()


def test_lineage_cli_renders_json_and_human_without_exposing_private_errors(
    tmp_path, monkeypatch
):
    store, db, source = _retry_world(tmp_path, monkeypatch)
    try:
        result, _ = retry_local_task_result(store, source.id)
        target_id = result["retry_contract_id"]
        before = (store.get_runtime_revision(), tuple(store.scan_runtime_records()))

        class Sync:
            def sync(self):
                return None

        monkeypatch.setattr(
            lineage_cli, "_persistent_snapshot", lambda: SQLiteStore(db)
        )
        monkeypatch.setattr(lineage_cli, "collaboration_service", lambda _store: Sync())
        rendered = CliRunner().invoke(task_group, ["lineage", source.id, "--json"])
        assert rendered.exit_code == 0, rendered.output
        payload = json.loads(rendered.output)
        assert target_id in payload["data"]["contract_ids"]
        human = CliRunner().invoke(task_group, ["lineage", source.id])
        assert human.exit_code == 0, human.output
        assert target_id in human.output
        assert "private provider detail" not in rendered.output + human.output
        assert (
            store.get_runtime_revision(),
            tuple(store.scan_runtime_records()),
        ) == before
    finally:
        store.close()


def test_lineage_requires_redis_and_does_not_fallback(monkeypatch):
    from aigineering.redis_collaboration import RedisCollaborationUnavailable

    class Snapshot:
        closed = False

        def close(self):
            self.closed = True

    snapshot = Snapshot()
    monkeypatch.setattr(lineage_cli, "_persistent_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        lineage_cli,
        "collaboration_service",
        lambda _store: (_ for _ in ()).throw(
            RedisCollaborationUnavailable("private endpoint")
        ),
    )
    result = CliRunner().invoke(task_group, ["lineage", "task:v6:sample", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output) == {"ok": False, "error": "redis_unavailable"}
    assert "private endpoint" not in result.output
    assert snapshot.closed


def test_real_tool_recovery_is_navigable_from_source_and_recovery(
    tmp_path, monkeypatch
):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *_a, **_kw: None)
    env, failure, recovery = _recovery_world(tmp_path)
    try:
        before = tuple(env.store.scan_runtime_records())
        revision = env.store.get_runtime_revision()
        source_view = task_lineage(env.store, env.source.id)["data"]
        recovery_view = task_lineage(env.store, recovery.id)["data"]
        assert source_view["contract_ids"] == recovery_view["contract_ids"]
        edge = next(
            (
                edge
                for edge in source_view["edges"]
                if edge["kind"] == "recovery_publication"
            ),
            None,
        )
        assert edge is not None, source_view["issues"]
        assert (edge["source_contract_id"], edge["target_contract_id"]) == (
            env.source.id,
            recovery.id,
        )
        assert failure.id in edge["evidence_record_ids"]
        assert edge["head_id"] in edge["evidence_record_ids"]
        assert env.consumer.id in source_view["contract_ids"]
        assert "PRIVATE:" not in json.dumps(source_view)
        assert tuple(env.store.scan_runtime_records()) == before
        assert env.store.get_runtime_revision() == revision
        digest = env.store.runtime_materialization_digest()
        reopened = SQLiteStore(env.database_path)
        try:
            assert reopened.rebuild_runtime_materializations(strict=True) == digest
            assert (
                task_lineage(reopened, recovery.id)["data"]["edges"]
                == source_view["edges"]
            )
        finally:
            reopened.close()
    finally:
        env.store.close()


def test_unverified_recovery_declaration_does_not_expand_lineage(tmp_path, monkeypatch):
    import aigineering.task_lineage as lineage_module
    from dataclasses import replace

    import aigineering.task_view_query as query_module

    monkeypatch.setattr(
        __import__("aigineering.runtime_services", fromlist=["sync_runtime"]),
        "sync_runtime",
        lambda *_a, **_kw: None,
    )
    env, _failure, recovery = _recovery_world(tmp_path)
    try:
        original = query_module.load_task_view_query_context

        def missing_recovery_member(store):
            query = original(store)
            rows = tuple(
                (revision, record)
                for revision, record in query.runtime_records
                if not (
                    record.record_type == "contract.declared"
                    and record.payload.get("contract", {}).get("id") == recovery.id
                )
            )
            return replace(query, runtime_records=rows)

        monkeypatch.setattr(
            lineage_module, "load_task_view_query_context", missing_recovery_member
        )
        result = task_lineage(env.store, env.source.id)["data"]
        assert recovery.id not in result["contract_ids"]
        assert any(
            issue.get("contract_id") == recovery.id for issue in result["issues"]
        )
    finally:
        env.store.close()


def test_parent_containment_requires_a_published_parent(tmp_path, monkeypatch):
    store, _db, source = _retry_world(tmp_path, monkeypatch)
    try:
        original = store.scan_runtime_records

        def without_funder_declaration():
            return tuple(
                pair
                for pair in original()
                if not (
                    pair[1].record_type == "contract.declared"
                    and pair[1].payload.get("contract", {}).get("name")
                    == "lineage funder"
                )
            )

        monkeypatch.setattr(store, "scan_runtime_records", without_funder_declaration)
        result = task_lineage(store, source.id)["data"]
        assert source.id in result["contract_ids"]
        assert not any(
            edge["kind"] == "containment" and edge["target_contract_id"] == source.id
            for edge in result["edges"]
        )
        assert any(
            issue["code"] == "containment_parent_unverified"
            and issue["contract_id"] == source.id
            for issue in result["issues"]
        )
    finally:
        store.close()


def test_historical_signed_handoff_is_read_only_and_rejects_materialization_drift(
    tmp_path, monkeypatch
):
    store, source, target = _historical_handoff_fixture(tmp_path)
    try:
        before_records = tuple(store.scan_runtime_records())
        before_revision = store.get_runtime_revision()
        source_view = task_lineage(store, source.id)["data"]
        target_view = task_lineage(store, target.id)["data"]
        assert source_view["contract_ids"] == target_view["contract_ids"]
        assert source_view["edges"] == target_view["edges"]
        edge = next(
            edge
            for edge in source_view["edges"]
            if edge["kind"] == "handoff_publication"
        )
        assert (edge["source_contract_id"], edge["target_contract_id"]) == (
            source.id,
            target.id,
        )
        assert edge["handoff_head_id"] in edge["evidence_record_ids"]
        assert tuple(store.scan_runtime_records()) == before_records
        assert store.get_runtime_revision() == before_revision

        original_get_all = store.get_all_contracts
        monkeypatch.setattr(
            store,
            "get_all_contracts",
            lambda: [
                replace(contract, name="changed historical materialization")
                if contract.id == target.id
                else contract
                for contract in original_get_all()
            ],
        )
        changed = task_lineage(store, source.id)["data"]
        assert not any(
            edge["kind"] == "handoff_publication" for edge in changed["edges"]
        )
        assert any(
            issue.get("contract_id") == target.id and "unverified" in issue["code"]
            for issue in changed["issues"]
        )
        assert tuple(store.scan_runtime_records()) == before_records
        assert store.get_runtime_revision() == before_revision
    finally:
        store.close()


def test_lineage_cycle_detection_is_iterative_and_returns_only_cycle_members():
    edges = [
        {"source_contract_id": f"task:{i}", "target_contract_id": f"task:{i + 1}"}
        for i in range(1200)
    ]
    assert _cycle_members(edges) == set()
    edges.extend(
        (
            {"source_contract_id": "task:1198", "target_contract_id": "task:1199"},
            {"source_contract_id": "task:1199", "target_contract_id": "task:1198"},
        )
    )
    assert _cycle_members(edges) == {"task:1198", "task:1199"}
