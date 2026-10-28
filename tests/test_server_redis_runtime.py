"""Required Redis HTTP health and post-commit failure semantics."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import (
    ActorKey,
    candidate_proposal_to_dict,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.protocol.types import Asset
from aigineering.server.app import app


def test_redis_health_does_not_create_workspace_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("aigineering.server.app.require_redis", lambda: object())

    response = TestClient(app).get("/health/redis")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "backend": "redis", "required": True}
    assert not (tmp_path / ".aig").exists()


def test_readiness_reports_lag_without_sync_or_rebuild(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SQLiteStore(".aig/store.db")
    store.close()
    monkeypatch.setattr("aigineering.server.app.require_redis", lambda: object())

    class ReadinessOnlyService:
        calls = 0

        def status(self):
            return {
                "available": True,
                "backend": "redis",
                "configured": True,
                "current": False,
                "authoritative_revision": 8,
                "watermark": 6,
                "lag": 2,
                "reason": "revision_lag",
                "schema": "v1",
            }

        def sync(self):
            self.calls += 1
            raise AssertionError("readiness must not synchronize")

        def rebuild(self):
            self.calls += 1
            raise AssertionError("readiness must not rebuild")

    service = ReadinessOnlyService()
    monkeypatch.setattr(
        "aigineering.server.app.collaboration_service", lambda _store: service
    )

    response = TestClient(app).get("/readiness/redis")

    assert response.status_code == 503
    assert response.json()["reason"] == "revision_lag"
    assert response.json()["lag"] == 2
    assert response.json()["ok"] is False
    assert service.calls == 0


def test_redis_health_returns_sanitized_503_when_required_service_is_down(
    monkeypatch,
):
    def unavailable():
        raise QueryProjectionUnavailable(
            "Redis collaboration service unavailable; check health"
        )

    monkeypatch.setattr("aigineering.server.app.require_redis", unavailable)

    response = TestClient(app).get("/health/redis")

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "error": "redis_unavailable",
        "facts_retained": False,
        "candidate_accepted": None,
    }


def test_postcommit_relay_failure_returns_pending_and_keeps_asset_fact(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    signer = Ed25519Signer()
    genesis = create_genesis_manifest(
        "http-test",
        [
            ActorKey(
                "test:root",
                "root-1",
                signer.kind,
                signer.signer_id,
                ("asset.publish",),
            )
        ],
        "policy:test",
    )
    store = SQLiteStore(".aig/store.db")
    initialize_genesis(store, genesis)
    store.close()
    monkeypatch.setattr("aigineering.server.app.require_redis", lambda: object())

    class InterruptedRelay:
        def sync(self):
            raise QueryProjectionUnavailable("relay interrupted")

    monkeypatch.setattr(
        "aigineering.runtime_services.collaboration_service",
        lambda _store: InterruptedRelay(),
    )
    content = "accepted before relay outage"
    asset = Asset(
        id=hash_asset_content("retained-asset", content),
        name="retained-asset",
        content=content,
        origin="human",
        trust_tier="human",
        definition_hash=hash_asset_definition("retained-asset"),
        content_hash=hash_asset_content("retained-asset", content),
    )
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id="test:root",
        key_id="root-1",
        effects=[asset_proposal_effect(asset)],
        signer=signer,
        idempotency_key="server-postcommit-pending-test",
    )

    response = TestClient(app).post(
        "/assets", json=candidate_proposal_to_dict(candidate)
    )

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "error": "redis_sync_pending",
        "facts_retained": True,
        "candidate_accepted": True,
    }
    persisted = SQLiteStore.snapshot_from_path(tmp_path / ".aig/store.db")
    try:
        stored_assets = persisted.get_assets_by_name(asset.name)
        assert len(stored_assets) == 1
        assert stored_assets[0].content == content
        assert any(
            record.record_type == "candidate.committed"
            for _, record in persisted.scan_runtime_records()
        )
    finally:
        persisted.close()
