"""Signed exact task attachment links and read-only byte export."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from dataclasses import fields
from click import Group
from click.testing import CliRunner
from dataclasses import replace
from pathlib import Path

import pytest

from aigineering.business import artifacts
from aigineering.cli.task_attachments import register_task_attachment_command
from aigineering.core.candidate_publisher import CandidateCommitter
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.domain import load_genesis
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.protocol.identity import contract_from_fields
from aigineering.local_identity import ensure_local_domain
from aigineering.local_publication import (
    commit_local_effect,
    commit_local_effects,
    require_accepted,
)
from aigineering.runtime_services import RedisSyncPending
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    create_candidate_proposal,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset
from aigineering.protocol.wire import contract_to_dict
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.task_attachments import LINKED, validate_link_history
from aigineering.task_attachments import (
    TaskAttachmentError,
    add_task_attachment,
    export_task_attachment,
    open_task_attachment_store,
    task_attachment_list,
    _encode_cursor,
)
from aigineering.task_publication import prepare_task_contract, publish_task_contract
from aigineering.task_views import project_task_status


class _Sync:
    def sync(self):
        return None


def _fixture(tmp_path: Path, monkeypatch):
    database = tmp_path / "store.db"
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / "identity" / "root.ed25519")
    )
    store = SQLiteStore(str(database))
    ensure_local_domain(store)
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_k: None
    )
    monkeypatch.setattr("aigineering.task_attachments.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.task_attachments.sync_runtime", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "aigineering.task_attachments.collaboration_service", lambda _store: _Sync()
    )
    contract = publish_task_contract(
        store, prepare_task_contract({"name": "attachments", "budget": 2})
    )
    marker = b"private attachment bytes\x00\xff"
    proposal = artifacts.attachment(
        "evidence.bin",
        marker,
        media_type="application/octet-stream",
        source_uri="private://never-export",
    )
    decision = require_accepted(
        commit_local_effect(
            store,
            asset_proposal_effect(proposal),
            idempotency_key=f"artifact:{proposal.id}",
        )
    )
    asset = decision.assets[0]
    return store, database, contract, asset, marker


def _table_fingerprint(path: Path):
    connection = sqlite3.connect(path)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        result = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            schema = tuple(connection.execute(f"PRAGMA table_info({quoted})"))
            rows = tuple(
                sorted(
                    (
                        repr(tuple(row))
                        for row in connection.execute(f"SELECT * FROM {quoted}")
                    )
                )
            )
            result[name] = hashlib.sha256(repr((schema, rows)).encode()).hexdigest()
        return result
    finally:
        connection.close()


def _bounded_child(parent, name):
    values = {
        field.name: getattr(parent, field.name)
        for field in fields(parent)
        if field.name != "id"
    }
    values.update(name=name, origin="plan", parent_id=parent.id, budget=1)
    return contract_from_fields(**values)


def test_signed_attachment_add_replay_list_and_exact_byte_export(tmp_path, monkeypatch):
    store, database, contract, asset, marker = _fixture(tmp_path, monkeypatch)
    before_revision = store.get_runtime_revision()
    status_before = project_task_status(contract, store)["status"]
    contract_before = contract_to_dict(store.get_contract(contract.id))
    disclosure_before = tuple(
        (asset.id, asset.content) for asset in compute_disclosure(contract, store)
    )
    claims_before = store.get_claim(contract.id)
    qualified_before = tuple(
        record.id
        for _, record in store.scan_runtime_records(record_type="output.qualified")
    )
    outputs_before = all_outputs_satisfied(contract, store)
    first, _revision = add_task_attachment(store, contract.id, asset.id)
    second, _revision = add_task_attachment(store, contract.id, asset.id)
    assert first["record_id"] == second["record_id"]
    assert first["candidate_id"] == second["candidate_id"]
    assert first["fact_ids"] == second["fact_ids"]
    assert project_task_status(contract, store)["status"] == status_before
    assert contract_to_dict(store.get_contract(contract.id)) == contract_before
    assert (
        tuple(
            (asset.id, asset.content) for asset in compute_disclosure(contract, store)
        )
        == disclosure_before
    )
    assert store.get_claim(contract.id) == claims_before
    assert (
        tuple(
            record.id
            for _, record in store.scan_runtime_records(record_type="output.qualified")
        )
        == qualified_before
    )
    assert all_outputs_satisfied(contract, store) == outputs_before

    counts_before_read = _table_fingerprint(database)
    listed = task_attachment_list(store, contract.id, limit=20)
    assert len(listed["data"]["attachments"]) == 1
    metadata = listed["data"]["attachments"][0]
    assert metadata["asset_id"] == asset.id
    assert metadata["record_id"] == first["record_id"]
    assert metadata["size"] == len(marker)
    assert metadata["sha256"] == __import__("hashlib").sha256(marker).hexdigest()
    serialized = json.dumps(listed)
    assert "private://never-export" not in serialized
    assert "private attachment bytes" not in serialized
    assert counts_before_read == _table_fingerprint(database)

    output = tmp_path / "export.bin"
    exported = export_task_attachment(store, contract.id, asset.id, output)
    assert output.read_bytes() == marker
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert exported["data"]["sha256"] == metadata["sha256"]
    assert counts_before_read == _table_fingerprint(database)
    assert store.get_runtime_revision() >= before_revision
    store.close()


def test_explicit_key_cannot_be_rebound_and_redacted_attachment_is_not_linkable(
    tmp_path, monkeypatch
):
    store, _database, contract, asset, _marker = _fixture(tmp_path, monkeypatch)
    add_task_attachment(store, contract.id, asset.id, idempotency_key="fixed-key")
    another = artifacts.attachment(
        "other.bin", b"another", media_type="application/octet-stream"
    )
    another_decision = require_accepted(
        commit_local_effect(
            store,
            asset_proposal_effect(another),
            idempotency_key=f"artifact:{another.id}",
        )
    )
    with pytest.raises(TaskAttachmentError) as rebound:
        add_task_attachment(
            store,
            contract.id,
            another_decision.assets[0].id,
            idempotency_key="fixed-key",
        )
    assert rebound.value.code == "candidate_rejected"

    redacted = replace(
        artifacts.attachment(
            "redacted.bin", b"hidden", media_type="application/octet-stream"
        ),
        disclosure_view="summary",
    )
    redacted_decision = require_accepted(
        commit_local_effect(
            store,
            asset_proposal_effect(redacted),
            idempotency_key=f"artifact:{redacted.id}",
        )
    )
    with pytest.raises(TaskAttachmentError) as unavailable:
        add_task_attachment(store, contract.id, redacted_decision.assets[0].id)
    assert unavailable.value.code == "candidate_rejected"
    store.close()


def test_links_support_multi_declaration_and_review_request_task_heads(
    tmp_path, monkeypatch
):
    store, _database, parent, attachment, _marker = _fixture(tmp_path, monkeypatch)

    children = (
        _bounded_child(parent, "planned child one"),
        _bounded_child(parent, "planned child two"),
    )
    multi = require_accepted(
        commit_local_effects(
            store,
            tuple(contract_declaration_effect(child) for child in children),
            idempotency_key="attachment-multi-declaration",
        )
    )
    assert (
        len(
            [
                record
                for record in multi.runtime_records
                if record.record_type == "contract.declared"
            ]
        )
        == 2
    )
    assert (
        len(
            [
                record
                for record in multi.runtime_records
                if record.record_type == "allowance.reserved"
            ]
        )
        == 2
    )
    multi_rows = tuple(record for _revision, record in store.scan_runtime_records())
    child_declaration = next(
        record
        for record in multi_rows
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == children[1].id
    )
    from aigineering.plugins.task_attachments import (
        validate_contract_declaration_member,
    )

    for missing_type in ("allowance.granted", "allowance.reserved"):
        omitted = next(
            record
            for record in multi_rows
            if record.record_type == missing_type
            and record.payload.get(
                "contract_id", record.payload.get("child_contract_id")
            )
            == children[1].id
        )
        with pytest.raises(ValueError):
            validate_contract_declaration_member(
                tuple(record for record in multi_rows if record.id != omitted.id),
                child_declaration,
                contract_id=children[1].id,
                domain_id=load_genesis(store).id,
            )
    add_task_attachment(store, children[1].id, attachment.id)
    child_link, _revision = add_task_attachment(store, children[1].id, attachment.id)
    assert child_link["asset_id"] == attachment.id

    source_template = prepare_task_contract(
        {
            "name": "review source",
            "outputs": ["final"],
            "budget": 3,
            "acceptance_policy": {
                "mode": "independent",
                "policy_version": "attachment-review-v1",
                "required_attestations": 1,
                "rubric_asset_ids": [],
                "evidence_asset_ids": [],
                "verifier_capabilities": ["review"],
            },
        }
    )
    source = source_template
    publish_task_contract(store, source)
    genesis = load_genesis(store)
    worker_signer = Ed25519Signer()
    worker = ActorKey(
        "worker:attachment-review-source",
        "worker-attachment-review-source",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    authorized = require_accepted(
        commit_local_effect(
            store,
            actor_authorization_effect(worker),
            idempotency_key="attachment-review-worker-authorize",
        )
    )
    assert authorized.accepted
    store.register_worker(
        WorkerRegistration(
            worker_id=worker.actor_id,
            actor_id=worker.actor_id,
            key_id=worker.key_id,
        )
    )
    claim = store.claim_contract(source.id, worker.actor_id, package_id="pkg:review")
    assert claim is not None
    output_candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=worker.actor_id,
        key_id=worker.key_id,
        effects=(
            asset_proposal_effect(
                Asset(id="untrusted", name="final", content="reviewable output")
            ),
        ),
        signer=worker_signer,
        idempotency_key="attachment-review-output",
        claim_binding=CandidateClaimBinding(
            contract_id=source.id,
            claim_id=claim["claim_id"],
            claim_epoch=claim["epoch"],
            package_id=claim["package_id"],
        ),
    )
    output_decision = CandidateCommitter(store, store).commit(output_candidate, genesis)
    assert output_decision.accepted
    output = output_decision.assets[0]
    monkeypatch.setattr("aigineering.task_policy_review.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.task_policy_review.sync_runtime", lambda *_a, **_k: None
    )
    from aigineering.task_policy_review import publish_task_review_request

    request, _revision = publish_task_review_request(
        store, source.id, "final", output.id, "Independent check"
    )
    reviewer_id = request["child_contract_id"]
    add_task_attachment(store, reviewer_id, attachment.id)
    reviewer_declaration = next(
        record
        for _revision, record in store.scan_runtime_records(
            record_type="contract.declared"
        )
        if record.payload["contract"]["id"] == reviewer_id
    )
    validate_contract_declaration_member(
        (record for _revision, record in store.scan_runtime_records()),
        reviewer_declaration,
        contract_id=reviewer_id,
        domain_id=load_genesis(store).id,
    )
    review_rows = tuple(record for _revision, record in store.scan_runtime_records())
    review_request = next(
        record
        for record in review_rows
        if record.record_type == "task.review.requested"
        and record.payload.get("child_contract_id") == reviewer_id
    )
    target_commit = next(
        record
        for record in review_rows
        if record.record_type == "asset.committed"
        and record.payload.get("asset", {}).get("id")
        == review_request.payload["asset_id"]
    )
    with pytest.raises(ValueError):
        validate_contract_declaration_member(
            tuple(record for record in review_rows if record.id != target_commit.id),
            reviewer_declaration,
            contract_id=reviewer_id,
            domain_id=load_genesis(store).id,
        )
    store.close()


def test_task_attachments_do_not_change_active_submitted_cancelled_or_completed_state(
    tmp_path, monkeypatch
):
    store, _database, base, asset, _marker = _fixture(tmp_path, monkeypatch)
    from aigineering.task_publication import publish_task_cancel

    active = publish_task_contract(
        store,
        prepare_task_contract(
            {"name": "active attachment target", "outputs": ["active-final"]}
        ),
    )
    submitted = publish_task_contract(
        store,
        prepare_task_contract(
            {"name": "submitted attachment target", "outputs": ["submitted-final"]}
        ),
    )
    cancelled = publish_task_contract(
        store,
        prepare_task_contract(
            {"name": "cancelled attachment target", "outputs": ["cancelled-final"]}
        ),
    )
    completed = publish_task_contract(
        store,
        prepare_task_contract(
            {"name": "completed attachment target", "outputs": ["completed-final"]}
        ),
    )

    for task in (active, submitted):
        store.register_worker(WorkerRegistration(worker_id=f"worker:{task.id}"))
        claim = store.claim_contract(task.id, f"worker:{task.id}")
        assert claim is not None
        if task.id == submitted.id:
            store.mark_claim_submitted(claim["claim_id"])

    genesis = load_genesis(store)
    signer = Ed25519Signer()
    worker_key = ActorKey(
        "worker:attachment-complete",
        "attachment-complete-key",
        signer.kind,
        signer.signer_id,
        ("worker.submit", "asset.publish"),
    )
    authorized = require_accepted(
        commit_local_effect(
            store,
            actor_authorization_effect(worker_key),
            idempotency_key="attachment-complete-worker-auth",
        )
    )
    assert authorized.accepted
    store.register_worker(
        WorkerRegistration(
            worker_id=worker_key.actor_id,
            actor_id=worker_key.actor_id,
            key_id=worker_key.key_id,
        )
    )
    claim = store.claim_contract(
        completed.id, worker_key.actor_id, package_id="pkg:attachment-complete"
    )
    assert claim is not None
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=worker_key.actor_id,
        key_id=worker_key.key_id,
        effects=(
            asset_proposal_effect(
                Asset(id="untrusted", name="completed-final", content="complete")
            ),
        ),
        signer=signer,
        idempotency_key="attachment-complete-output",
        claim_binding=CandidateClaimBinding(
            contract_id=completed.id,
            claim_id=claim["claim_id"],
            claim_epoch=claim["epoch"],
            package_id=claim["package_id"],
        ),
    )
    result = CandidateCommitter(store, store).commit(candidate, genesis)
    assert result.accepted, result

    # The active target above already owns a claim; keep the state established
    # before linking so this control tests metadata neutrality, not readiness.
    monkeypatch.setattr(
        "aigineering.runtime_services.sync_runtime", lambda *_a, **_k: None
    )
    publish_task_cancel(store, cancelled.id, "cancel before attachment")
    tasks = (active, submitted, cancelled, completed)
    before = {
        task.id: (
            project_task_status(task, store)["status"],
            contract_to_dict(store.get_contract(task.id)),
            store.get_claim(task.id),
            all_outputs_satisfied(task, store),
        )
        for task in tasks
    }
    assert [row[0] for row in before.values()] == [
        "claimed",
        "submitted",
        "cancelled",
        "completed",
    ]
    assert store.get_claim(active.id)["status"] == "active"
    for task in tasks:
        add_task_attachment(store, task.id, asset.id)
    after = {
        task.id: (
            project_task_status(task, store)["status"],
            contract_to_dict(store.get_contract(task.id)),
            store.get_claim(task.id),
            all_outputs_satisfied(task, store),
        )
        for task in tasks
    }
    assert after == before
    store.close()


def test_handoff_and_revision_successor_declarations_use_real_publisher_heads(
    tmp_path, monkeypatch
):
    store, _database, _base, attachment, _marker = _fixture(tmp_path, monkeypatch)
    source = publish_task_contract(
        store,
        prepare_task_contract(
            {"name": "handoff attachment source", "outputs": ["final"]}
        ),
    )
    monkeypatch.setattr("aigineering.task_handoff.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.task_handoff.sync_runtime", lambda *_a, **_k: None)
    from aigineering.task_handoff import publish_task_handoff
    from aigineering.plugins.task_attachments import (
        validate_contract_declaration_member,
    )

    handoff, _revision = publish_task_handoff(
        store, source.id, "continue through signed handoff"
    )
    handoff_successor = handoff["successor_id"]
    add_task_attachment(store, handoff_successor, attachment.id)
    rows = tuple(record for _revision, record in store.scan_runtime_records())
    handoff_declaration = next(
        record
        for record in rows
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == handoff_successor
    )
    validate_contract_declaration_member(
        rows,
        handoff_declaration,
        contract_id=handoff_successor,
        domain_id=load_genesis(store).id,
    )
    handoff_receipt, handoff_head = validate_contract_declaration_member(
        rows,
        handoff_declaration,
        contract_id=handoff_successor,
        domain_id=load_genesis(store).id,
    )
    del handoff_receipt, handoff_head
    terminal = next(
        record
        for record in rows
        if record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == source.id
    )
    with pytest.raises(ValueError):
        validate_contract_declaration_member(
            tuple(record for record in rows if record.id != terminal.id),
            handoff_declaration,
            contract_id=handoff_successor,
            domain_id=load_genesis(store).id,
        )
    # Keep an internally complete Candidate head but make the relation point
    # at an unrelated terminal. This used to pass the old "any terminal" test.
    from aigineering.core.lifecycle_facts import create_terminal_record

    handoff_relation = next(
        record
        for record in rows
        if record.record_type == "task.handed_off"
        and record.payload.get("successor_id") == handoff_successor
    )
    receipt = next(
        record
        for record in rows
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id")
        == handoff_relation.payload["candidate_id"]
    )
    head = next(
        record
        for record in rows
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id")
        == handoff_relation.payload["candidate_id"]
    )
    unrelated_terminal = create_terminal_record(
        "task:v6:unrelated-descendant",
        "cancelled",
        actor_id=receipt.payload["actor_id"],
        causal_parents=(receipt.id,),
    )
    forged_relation = create_runtime_record(
        "task.handed_off",
        dict(handoff_relation.payload),
        causal_parents=tuple(
            unrelated_terminal.id if item == terminal.id else item
            for item in handoff_relation.causal_parents
        ),
    )
    member_ids = tuple(
        forged_relation.id if item == handoff_relation.id else item
        for item in head.payload["committed_record_ids"]
    ) + (unrelated_terminal.id,)
    forged_head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": head.payload["candidate_id"],
            "committed_record_ids": member_ids,
        },
        causal_parents=(receipt.id, *member_ids),
    )
    forged_rows = tuple(
        record for record in rows if record.id not in {handoff_relation.id, head.id}
    ) + (unrelated_terminal, forged_relation, forged_head)
    with pytest.raises(ValueError, match="exact source terminal"):
        validate_contract_declaration_member(
            forged_rows,
            handoff_declaration,
            contract_id=handoff_successor,
            domain_id=load_genesis(store).id,
        )
    store.close()

    # Reuse the existing ordinary Worker/reviewer fixture so the revision
    # Candidate has a real output, review request, report and signed decision.
    import importlib.util

    revision_support = importlib.util.spec_from_file_location(
        "task_revision_test_support", Path(__file__).with_name("test_task_revision.py")
    )
    assert revision_support is not None and revision_support.loader is not None
    revision_module = importlib.util.module_from_spec(revision_support)
    revision_support.loader.exec_module(revision_module)
    _commit = revision_module._commit
    _env = revision_module._env
    from aigineering.plugins.task_revision import TASK_REVISION_EFFECTS
    from aigineering.plugins.task_review import TASK_REVIEW_EFFECTS
    from aigineering.protocol.effect_builders import task_revision_effect

    revision_path = tmp_path / "revision"
    revision_path.mkdir()
    revision_env = _env(revision_path)
    revision_store = revision_env["store"]
    revision_result = _commit(
        revision_store,
        revision_env["genesis"],
        revision_env["signer"],
        revision_env["operator"],
        task_revision_effect(
            revision_env["decision"].id, "continue through signed revision"
        ),
        "attachment-revision-successor",
        effects={**TASK_REVIEW_EFFECTS, **TASK_REVISION_EFFECTS},
    )
    assert revision_result.accepted, revision_result
    revision_link = next(
        record
        for record in revision_result.runtime_records
        if record.record_type == "task.revised"
    )
    revision_successor = revision_env["store"].get_contract(
        revision_link.payload["successor_id"]
    )
    assert revision_successor is not None
    declaration = next(
        record
        for record in revision_result.runtime_records
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == revision_successor.id
    )
    validate_contract_declaration_member(
        tuple(record for _revision, record in revision_store.scan_runtime_records()),
        declaration,
        contract_id=revision_successor.id,
        domain_id=revision_env["genesis"].id,
    )
    revision_rows = tuple(
        record for _revision, record in revision_store.scan_runtime_records()
    )
    source_terminal = next(
        record
        for record in revision_rows
        if record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == revision_env["source"].id
    )
    with pytest.raises(ValueError):
        validate_contract_declaration_member(
            tuple(
                record for record in revision_rows if record.id != source_terminal.id
            ),
            declaration,
            contract_id=revision_successor.id,
            domain_id=revision_env["genesis"].id,
        )
    attachment_proposal = artifacts.attachment(
        "revision-source.bin",
        b"revision attachment",
        media_type="application/octet-stream",
    )
    attachment_result = _commit(
        revision_store,
        revision_env["genesis"],
        revision_env["signer"],
        revision_env["operator"],
        asset_proposal_effect(attachment_proposal),
        "attachment-revision-source-asset",
    )
    assert attachment_result.accepted, attachment_result
    revision_attachment_id = attachment_result.assets[0].id
    link_actor = ActorKey(
        "human:attachment-linker",
        "attachment-linker-key",
        revision_env["signer"].kind,
        revision_env["signer"].signer_id,
        ("task.attach",),
    )
    authorization = _commit(
        revision_store,
        revision_env["genesis"],
        revision_env["signer"],
        revision_env["genesis"].root_keys[0],
        actor_authorization_effect(link_actor),
        "attachment-revision-linker-auth",
    )
    assert authorization.accepted, authorization
    from aigineering.core.candidate_publisher import CandidatePublisher

    monkeypatch.setattr(
        "aigineering.task_attachments.ensure_local_plugin_publisher",
        lambda _store, _plugin_id, _capabilities: CandidatePublisher(
            revision_store,
            revision_store,
            revision_env["genesis"],
            link_actor,
            revision_env["signer"],
        ),
    )
    monkeypatch.setattr("aigineering.task_attachments.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.task_attachments.sync_runtime", lambda *_a, **_k: None
    )
    linked_revision, _revision = add_task_attachment(
        revision_store, revision_successor.id, revision_attachment_id
    )
    assert linked_revision["asset_id"] == revision_attachment_id
    revision_store.close()


def test_postcommit_redis_pending_retains_candidate_and_exact_replay_recovers(
    tmp_path, monkeypatch
):
    store, _database, contract, asset, _marker = _fixture(tmp_path, monkeypatch)
    calls = 0

    def pending_after_commit(_store, *, accepted=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RedisSyncPending(accepted=accepted)

    monkeypatch.setattr(
        "aigineering.task_attachments.sync_runtime", pending_after_commit
    )
    with pytest.raises(RedisSyncPending) as pending:
        add_task_attachment(store, contract.id, asset.id)
    assert pending.value.accepted is True
    assert pending.value.decision is not None
    assert pending.value.data["fact_ids"]
    original_ids = pending.value.data["fact_ids"]
    assert (
        len(
            validate_link_history(
                (row for _, row in store.scan_runtime_records()), contract.id
            )
        )
        == 1
    )

    monkeypatch.setattr(
        "aigineering.task_attachments.sync_runtime", lambda *_a, **_k: None
    )
    replay, _revision = add_task_attachment(store, contract.id, asset.id)
    assert replay["fact_ids"] == original_ids
    assert (
        len(
            validate_link_history(
                (row for _, row in store.scan_runtime_records()), contract.id
            )
        )
        == 1
    )
    store.close()


def test_unavailable_current_operator_key_stops_before_candidate_publication(
    tmp_path, monkeypatch
):
    store, _database, contract, asset, _marker = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "aigineering.task_attachments.ensure_local_plugin_publisher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("private detail")),
    )
    with pytest.raises(TaskAttachmentError) as unavailable:
        add_task_attachment(store, contract.id, asset.id)
    assert unavailable.value.code == "authorization_unavailable"
    records = tuple(record for _revision, record in store.scan_runtime_records())
    assert not any(record.record_type == LINKED for record in records)
    store.close()


def test_historic_link_remains_listed_when_current_asset_is_tombstoned_but_export_fails(
    tmp_path, monkeypatch
):
    store, _database, contract, asset, _marker = _fixture(tmp_path, monkeypatch)
    original, _revision = add_task_attachment(store, contract.id, asset.id)
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
    replay, _revision = add_task_attachment(store, contract.id, asset.id)
    assert replay["record_id"] == original["record_id"]
    assert replay["fact_ids"] == original["fact_ids"]
    listed = task_attachment_list(store, contract.id)
    assert listed["data"]["attachments"][0]["asset_id"] == asset.id
    with pytest.raises(TaskAttachmentError) as unavailable:
        export_task_attachment(
            store, contract.id, asset.id, tmp_path / "tombstoned.bin"
        )
    assert unavailable.value.code == "attachment_unavailable"
    store.close()


def test_custom_store_resolution_refuses_missing_paths_without_creating_them(tmp_path):
    missing = tmp_path / "missing" / "store.db"
    with pytest.raises(TaskAttachmentError) as unavailable:
        open_task_attachment_store(missing, writable=True)
    assert unavailable.value.code == "store_unavailable"
    assert not missing.parent.exists()


def test_cli_returns_safe_json_for_missing_store_without_creating_it(tmp_path):
    cli = Group()
    register_task_attachment_command(cli)
    missing = tmp_path / "never-created" / "store.db"
    result = CliRunner().invoke(
        cli,
        ["attachment", "list", "task:v6:missing", "--store", str(missing), "--json"],
    )
    assert result.exit_code == 1
    assert json.loads(result.output) == {"ok": False, "error": "store_unavailable"}
    assert not missing.parent.exists()


def test_partial_export_failure_keeps_only_the_created_partial_file_and_never_overwrites(
    tmp_path, monkeypatch
):
    store, _database, contract, asset, marker = _fixture(tmp_path, monkeypatch)
    add_task_attachment(store, contract.id, asset.id)
    destination = tmp_path / "partial.bin"
    real_write = os.write
    calls = 0

    def fail_after_prefix(fd, value):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, value[:3])
        raise OSError("controlled partial write")

    monkeypatch.setattr("aigineering.task_attachments.os.write", fail_after_prefix)
    with pytest.raises(TaskAttachmentError) as failed:
        export_task_attachment(store, contract.id, asset.id, destination)
    assert failed.value.code == "export_failed"
    assert destination.read_bytes() == marker[:3]
    with pytest.raises(TaskAttachmentError) as retry:
        export_task_attachment(store, contract.id, asset.id, destination)
    assert retry.value.code == "destination_exists"
    assert destination.read_bytes() == marker[:3]
    store.close()


def test_attachment_validation_fails_closed_for_wrong_kind_and_forged_history(
    tmp_path, monkeypatch
):
    store, _database, contract, asset, _marker = _fixture(tmp_path, monkeypatch)
    document = artifacts.document(
        "not-attachment",
        asset,
        {
            "schema": "document-extraction-v1",
            "tool": "fixture",
            "version": "1",
            "ocr": False,
            "pages": [{"page": 1, "text": "x"}],
        },
    )
    decision = require_accepted(
        commit_local_effect(
            store,
            asset_proposal_effect(document),
            idempotency_key=f"artifact:{document.id}",
        )
    )
    with pytest.raises(TaskAttachmentError):
        add_task_attachment(store, contract.id, decision.assets[0].id)
    assert not any(
        record.record_type == LINKED for _, record in store.scan_runtime_records()
    )

    links = validate_link_history(
        (record for _, record in store.scan_runtime_records()), contract.id
    )
    assert len(links) == 0
    store.append_runtime_record(
        create_runtime_record(
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
            },
            causal_parents=(
                "record:fake",
                "record:fake",
                "record:fake",
                "record:fake",
                "record:fake",
            ),
        )
    )
    with pytest.raises(ValueError):
        validate_link_history(
            (record for _, record in store.scan_runtime_records()), contract.id
        )
    store.close()


def test_export_never_overwrites_existing_file_or_exports_unlinked_asset(
    tmp_path, monkeypatch
):
    store, _database, contract, asset, _marker = _fixture(tmp_path, monkeypatch)
    output = tmp_path / "existing"
    output.write_bytes(b"keep")
    with pytest.raises(TaskAttachmentError) as existing:
        export_task_attachment(store, contract.id, asset.id, output)
    assert existing.value.code == "attachment_not_linked"
    add_task_attachment(store, contract.id, asset.id)
    with pytest.raises(TaskAttachmentError) as denied:
        export_task_attachment(store, contract.id, asset.id, output)
    assert denied.value.code == "destination_exists"
    assert output.read_bytes() == b"keep"
    store.close()


def test_list_cursor_is_scope_bound_and_fixes_its_watermark(tmp_path, monkeypatch):
    store, _database, contract, asset, _marker = _fixture(tmp_path, monkeypatch)
    add_task_attachment(store, contract.id, asset.id)
    second = artifacts.attachment(
        "second.bin", b"second", media_type="application/octet-stream"
    )
    second_decision = require_accepted(
        commit_local_effect(
            store,
            asset_proposal_effect(second),
            idempotency_key=f"artifact:{second.id}",
        )
    )
    add_task_attachment(store, contract.id, second_decision.assets[0].id)
    first_page = task_attachment_list(store, contract.id, limit=1)
    assert first_page["data"]["next_cursor"]
    token = first_page["data"]["next_cursor"]
    cursor_payload = {
        "v": 99,
        "domain_id": first_page["data"]["domain_id"],
        "contract_id": contract.id,
        "limit": 1,
        "watermark": first_page["data"]["watermark"],
        "revision": first_page["data"]["attachments"][0]["revision"],
        "record_id": first_page["data"]["attachments"][0]["record_id"],
    }
    for version in (99, True):
        cursor_payload["v"] = version
        with pytest.raises(TaskAttachmentError) as invalid:
            task_attachment_list(
                store, contract.id, limit=1, cursor=_encode_cursor(cursor_payload)
            )
        assert invalid.value.code == "invalid_cursor"
    later = artifacts.attachment(
        "later.bin", b"later", media_type="application/octet-stream"
    )
    later_decision = require_accepted(
        commit_local_effect(
            store, asset_proposal_effect(later), idempotency_key=f"artifact:{later.id}"
        )
    )
    add_task_attachment(store, contract.id, later_decision.assets[0].id)
    next_page = task_attachment_list(store, contract.id, limit=1, cursor=token)
    assert next_page["data"]["watermark"] == first_page["data"]["watermark"]
    assert len(next_page["data"]["attachments"]) == 1
    with pytest.raises(TaskAttachmentError) as mismatch:
        task_attachment_list(store, "task:v6:foreign", limit=1, cursor=token)
    assert mismatch.value.code in {"task_not_found", "cursor_scope_mismatch"}
    store.close()
