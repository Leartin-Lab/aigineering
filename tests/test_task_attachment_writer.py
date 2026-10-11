"""Writer-time revalidation for signed task attachment associations."""

from __future__ import annotations

import sqlite3

import pytest

from aigineering.business import artifacts
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.domain import load_genesis
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import (
    ensure_local_domain,
    ensure_local_plugin_publisher,
)
from aigineering.local_publication import commit_local_effect, require_accepted
from aigineering.plugins.task_attachments import LINKED
from aigineering.protocol.effect_builders import (
    actor_revocation_effect,
    asset_proposal_effect,
)
from aigineering.task_attachments import TaskAttachmentError, add_task_attachment
from aigineering.task_publication import prepare_task_contract, publish_task_contract
from aigineering.task_views import project_task_status


_PLUGIN_ID = "task.attachments.operator.v1"


def _workspace(tmp_path, monkeypatch):
    database = tmp_path / "attachment-writer.sqlite"
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / "identity" / "root.ed25519")
    )
    monkeypatch.setattr("aigineering.application.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_k: None
    )
    monkeypatch.setattr("aigineering.task_attachments.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.task_attachments.sync_runtime", lambda *_a, **_k: None
    )

    store = SQLiteStore(database)
    ensure_local_domain(store)
    contract = publish_task_contract(
        store, prepare_task_contract({"name": "writer-race", "budget": 2})
    )
    asset = artifacts.attachment(
        "writer-race.bin", b"original attachment", media_type="application/octet-stream"
    )
    published = require_accepted(
        commit_local_effect(
            store,
            asset_proposal_effect(asset),
            idempotency_key=f"writer-race-asset:{asset.id}",
        )
    )
    asset = published.assets[0]
    plugin = ensure_local_plugin_publisher(store, _PLUGIN_ID, ("task.attach",))
    second = SQLiteStore(database)
    return store, second, database, contract, asset, plugin


def _candidate_rows(call_kwargs):
    rows = tuple(call_kwargs["runtime_records"])
    linked = tuple(record for record in rows if record.record_type == LINKED)
    assert len(linked) == 1
    return rows, linked[0], call_kwargs["candidate_id"]


def test_revoked_attachment_operator_is_rechecked_inside_writer(tmp_path, monkeypatch):
    store, concurrent, _database, contract, asset, plugin = _workspace(
        tmp_path, monkeypatch
    )
    original_commit = store.commit_ingress_batch
    injected = False
    before_status = project_task_status(contract, store)["status"]
    before_claim = store.get_claim(contract.id)
    before_links = tuple(store.scan_runtime_records(record_type=LINKED))
    projected_candidate_ids: list[str] = []

    def revoke_after_preflight(**kwargs):
        nonlocal injected
        if not any(
            record.record_type == LINKED for record in kwargs.get("runtime_records", ())
        ):
            return original_commit(**kwargs)
        _rows, linked, candidate_id = _candidate_rows(kwargs)
        projected_candidate_ids.append(candidate_id)
        assert linked.payload["contract_id"] == contract.id
        assert linked.payload["asset_id"] == asset.id
        assert not injected
        injected = True

        genesis = load_genesis(concurrent)
        accepted = require_accepted(
            commit_local_effect(
                concurrent,
                actor_revocation_effect(
                    plugin.actor_key.actor_id,
                    plugin.actor_key.key_id,
                    "writer-race test revocation",
                ),
                idempotency_key="writer-race-revoke-attachment-operator",
            )
        )
        assert accepted.accepted
        assert any(
            key.actor_id == plugin.actor_key.actor_id
            and key.key_id == plugin.actor_key.key_id
            and key.revoked
            for key in load_effective_actor_keys(concurrent, genesis)
        )
        # The next call enters SQLite's writer transaction and runs the frozen
        # projector again against the revoked authorization state.
        return original_commit(**kwargs)

    monkeypatch.setattr(store, "commit_ingress_batch", revoke_after_preflight)
    try:
        with pytest.raises(TaskAttachmentError) as rejected:
            add_task_attachment(store, contract.id, asset.id)

        assert injected
        assert rejected.value.code == "candidate_rejected"
        assert len(projected_candidate_ids) == 1
        assert tuple(store.scan_runtime_records(record_type=LINKED)) == before_links
        assert store.get_claim(contract.id) == before_claim
        assert project_task_status(contract, store)["status"] == before_status
        records = tuple(record for _, record in store.scan_runtime_records())
        assert any(
            record.record_type == "actor.revoked"
            and record.payload.get("actor_id") == plugin.actor_key.actor_id
            and record.payload.get("key_id") == plugin.actor_key.key_id
            for record in records
        )
        assert any(
            record.record_type == "candidate.authentication_rejected"
            and record.payload.get("claimed_candidate_id") == projected_candidate_ids[0]
            for record in records
        )
        assert not any(
            record.record_type == "candidate.committed"
            and record.payload.get("candidate_id") == projected_candidate_ids[0]
            for record in records
        )
    finally:
        concurrent.close()
        store.close()


def test_changed_materialized_asset_is_rechecked_inside_writer(tmp_path, monkeypatch):
    store, concurrent, database, contract, asset, _plugin = _workspace(
        tmp_path, monkeypatch
    )
    original_commit = store.commit_ingress_batch
    injected = False
    before_status = project_task_status(contract, store)["status"]
    before_claim = store.get_claim(contract.id)
    before_links = tuple(store.scan_runtime_records(record_type=LINKED))
    projected_candidate_ids: list[str] = []

    def corrupt_materialized_asset_after_preflight(**kwargs):
        nonlocal injected
        if not any(
            record.record_type == LINKED for record in kwargs.get("runtime_records", ())
        ):
            return original_commit(**kwargs)
        _rows, linked, candidate_id = _candidate_rows(kwargs)
        projected_candidate_ids.append(candidate_id)
        assert linked.payload["contract_id"] == contract.id
        assert linked.payload["asset_id"] == asset.id
        assert not injected
        injected = True

        # There is no signed Asset replacement operation for an existing
        # immutable Asset ID. This isolated SQL fixture models corruption of
        # the materialized Asset row; it does not claim a supported mutation.
        with sqlite3.connect(database) as connection:
            cursor = connection.execute(
                "UPDATE assets SET content = ? WHERE id = ?",
                ('{"kind":"attachment","bytes":"Y29ycnVwdGVk"}', asset.id),
            )
            assert cursor.rowcount == 1

        assert concurrent.get_asset(asset.id).content != asset.content
        # The ordinary commit path now reprojects from the Store's current
        # state while holding its writer lock and must reject the stale plan.
        return original_commit(**kwargs)

    monkeypatch.setattr(
        store, "commit_ingress_batch", corrupt_materialized_asset_after_preflight
    )
    try:
        with pytest.raises(TaskAttachmentError) as rejected:
            add_task_attachment(store, contract.id, asset.id)

        assert injected
        assert rejected.value.code == "candidate_rejected"
        assert len(projected_candidate_ids) == 1
        assert tuple(store.scan_runtime_records(record_type=LINKED)) == before_links
        assert store.get_claim(contract.id) == before_claim
        assert project_task_status(contract, store)["status"] == before_status
        records = tuple(record for _, record in store.scan_runtime_records())
        assert any(
            record.record_type == "candidate.rejected"
            and record.payload.get("candidate_id") == projected_candidate_ids[0]
            for record in records
        )
        assert not any(
            record.record_type == "candidate.committed"
            and record.payload.get("candidate_id") == projected_candidate_ids[0]
            for record in records
        )
    finally:
        concurrent.close()
        store.close()
