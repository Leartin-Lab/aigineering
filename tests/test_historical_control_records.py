"""Read-only checks for signed historical control records."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from click import Group
from click.testing import CliRunner

from aigineering.business import artifacts
from aigineering.cli.task_attachments import register_task_attachment_command
from aigineering.core.publication_history import validate_published_member
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain, load_actor_signer
from aigineering.local_publication import commit_local_effects, require_accepted
from aigineering.plugins.task_attachments import LINKED
from aigineering.protocol.candidate import (
    CandidateEffect,
    candidate_received_record,
    create_candidate_proposal,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.wire import contract_to_dict
from aigineering.task_attachments import (
    TaskAttachmentError,
    export_task_attachment,
    open_task_attachment_store,
    task_attachment_list,
)
from aigineering.task_publication import prepare_task_contract


class _NoopSync:
    def sync(self):
        return None


def _historical_attachment(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "store.db"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(tmp_path / "identity/root.ed25519"))
    store = SQLiteStore(db_path)
    genesis = ensure_local_domain(store)
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        "aigineering.task_attachments.collaboration_service", lambda _store: _NoopSync()
    )

    contract = prepare_task_contract(
        {"name": "historical attachment reader", "budget": 2}
    )
    marker = b"historical attachment bytes\x00\xff"
    proposal = artifacts.attachment(
        "evidence.bin",
        marker,
        media_type="application/octet-stream",
        source_uri="private://historical-source",
    )
    decision = require_accepted(
        commit_local_effects(
            store,
            (
                contract_declaration_effect(contract),
                asset_proposal_effect(proposal),
            ),
            idempotency_key=f"mixed-contract-asset:{proposal.id}",
        )
    )
    contract = decision.contracts[0]
    asset = decision.assets[0]
    records = tuple(record for _revision, record in store.scan_runtime_records())
    contract_record = next(
        row
        for row in records
        if row.record_type == "contract.declared"
        and row.payload.get("contract", {}).get("id") == contract.id
    )
    asset_record = next(
        row
        for row in records
        if row.record_type == "asset.committed"
        and row.payload.get("asset", {}).get("id") == asset.id
    )
    _contract_receipt, contract_head = validate_published_member(
        records, contract_record, domain_id=genesis.id
    )
    asset_receipt, asset_head = validate_published_member(
        records, asset_record, domain_id=genesis.id
    )

    key = genesis.root_keys[0]
    witnesses = (
        contract_record.id,
        asset_record.id,
        asset_receipt.id,
        asset_head.id,
    )
    link_payload = {
        "actor_id": key.actor_id,
        "asset_candidate_id": asset_receipt.payload["candidate_id"],
        "asset_head_id": asset_head.id,
        "asset_id": asset.id,
        "asset_receipt_id": asset_receipt.id,
        "asset_record_id": asset_record.id,
        "contract_id": contract.id,
        "contract_record_id": contract_record.id,
        "key_id": key.key_id,
        "idempotency_key": "historical-attachment-link-v1",
    }
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=(CandidateEffect("task.attachment.link", link_payload),),
        signer=load_actor_signer(),
        causal_parents=witnesses,
        idempotency_key="historical-attachment-link-v1",
    )
    receipt = candidate_received_record(candidate, genesis)
    link_payload["candidate_id"] = candidate.id
    link = create_runtime_record(
        LINKED, link_payload, causal_parents=(receipt.id, *witnesses)
    )
    from aigineering.core.publication_history import candidate_commit_head

    head = candidate_commit_head(receipt, (link,))
    for row in (receipt, link, head):
        store.append_runtime_record(row)
    return store, db_path, contract, asset, marker, link


def _record_snapshot(store):
    return tuple(
        (revision, record.id, record.record_type, record.payload, record.causal_parents)
        for revision, record in store.scan_runtime_records()
    )


def test_historical_signed_attachment_is_readable_and_exports_exact_bytes(
    tmp_path, monkeypatch
):
    store, _db_path, contract, asset, marker, link = _historical_attachment(
        tmp_path, monkeypatch
    )
    before_revision = store.get_runtime_revision()
    before = _record_snapshot(store)
    listed = task_attachment_list(store, contract.id, limit=20)
    assert listed["data"]["attachments"][0]["record_id"] == link.id
    metadata = listed["data"]["attachments"][0]
    assert metadata["asset_id"] == asset.id
    assert metadata["size"] == len(marker)
    assert metadata["sha256"] == hashlib.sha256(marker).hexdigest()
    serialized = json.dumps(listed)
    assert "private://historical-source" not in serialized
    assert "historical attachment bytes" not in serialized

    destination = tmp_path / "export.bin"
    exported = export_task_attachment(store, contract.id, asset.id, destination)
    assert destination.read_bytes() == marker
    assert os.stat(destination).st_mode & 0o777 == 0o600
    assert exported["data"]["sha256"] == metadata["sha256"]
    assert _record_snapshot(store) == before
    assert store.get_runtime_revision() == before_revision
    assert contract_to_dict(store.get_contract(contract.id)) == contract_to_dict(
        contract
    )
    store.close()


def test_historical_attachment_export_rejects_tombstone_and_existing_path(
    tmp_path, monkeypatch
):
    store, _db_path, contract, asset, _marker, _link = _historical_attachment(
        tmp_path, monkeypatch
    )
    existing = tmp_path / "existing.bin"
    existing.write_bytes(b"preserve")
    with pytest.raises(TaskAttachmentError, match="destination_exists"):
        export_task_attachment(store, contract.id, asset.id, existing)
    assert existing.read_bytes() == b"preserve"

    original_get_asset = store.get_asset
    monkeypatch.setattr(
        store,
        "get_asset",
        lambda asset_id: (
            replace(original_get_asset(asset_id), tombstoned=True)
            if original_get_asset(asset_id) is not None
            else None
        ),
    )
    with pytest.raises(TaskAttachmentError, match="attachment_unavailable"):
        export_task_attachment(
            store, contract.id, asset.id, tmp_path / "tombstoned.bin"
        )
    store.close()


def test_attachment_reader_rejects_forged_history_and_missing_store(
    tmp_path, monkeypatch
):
    store, db_path, contract, asset, _marker, _link = _historical_attachment(
        tmp_path, monkeypatch
    )
    forged = create_runtime_record(
        LINKED,
        {
            "actor_id": "forged",
            "asset_candidate_id": "candidate:fake",
            "asset_head_id": "record:fake",
            "asset_id": asset.id,
            "asset_receipt_id": "record:fake",
            "asset_record_id": "record:fake",
            "candidate_id": "candidate:fake",
            "contract_id": contract.id,
            "contract_record_id": "record:fake",
            "key_id": "fake",
            "idempotency_key": "fake-key",
        },
        causal_parents=("record:fake",) * 5,
    )
    store.append_runtime_record(forged)
    with pytest.raises(ValueError):
        task_attachment_list(store, contract.id)
    store.close()

    missing = tmp_path / "missing" / "store.db"
    with pytest.raises(TaskAttachmentError, match="store_unavailable"):
        open_task_attachment_store(missing, writable=True)
    assert not missing.parent.exists()

    cli = Group()
    register_task_attachment_command(cli)
    result = CliRunner().invoke(
        cli,
        ["attachment", "list", "task:v6:missing", "--store", str(missing), "--json"],
    )
    assert result.exit_code == 1
    assert json.loads(result.output) == {"ok": False, "error": "store_unavailable"}


def test_attachment_reader_requires_task_materialization_to_match_history(
    tmp_path, monkeypatch
):
    store, _db_path, contract, _asset, _marker, _link = _historical_attachment(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        store,
        "get_contract",
        lambda contract_id: (
            replace(contract, description="changed materialization")
            if contract_id == contract.id
            else None
        ),
    )
    with pytest.raises(TaskAttachmentError, match="task_history_unavailable"):
        task_attachment_list(store, contract.id)
    store.close()
