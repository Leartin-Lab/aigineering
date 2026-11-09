"""CLI coverage for review compilation into signed ordinary effects."""

from __future__ import annotations

import json
from pathlib import Path

from click import Group
from click.testing import CliRunner

from aigineering.cli.task_policy_review import register_task_policy_review_commands
from aigineering.core.candidate_publisher import CandidatePublisher, publish_effect
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Asset


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_policy_review_commands(task)
    root.add_command(task)
    return root


def _setup(tmp_path: Path, monkeypatch):
    import aigineering.application
    import aigineering.cli.task_policy_review as cli
    import aigineering.task_policy_review as service
    import aigineering.runtime_services as runtime_services

    database = tmp_path / "review.sqlite"
    store = SQLiteStore(database)
    signer = Ed25519Signer()
    operator = ActorKey(
        "human:operator",
        "operator-key",
        signer.kind,
        signer.signer_id,
        ("asset.attest", "asset.publish", "contract.publish", "verify.human"),
    )
    report_actor = ActorKey(
        "human:reporter",
        "report-key",
        signer.kind,
        signer.signer_id,
        ("asset.publish",),
    )
    target_actor = ActorKey(
        "human:target-publisher",
        "target-key",
        signer.kind,
        signer.signer_id,
        ("asset.publish",),
    )
    genesis = create_genesis_manifest(
        "review-cli", (operator, report_actor, target_actor), "policy:test"
    )
    initialize_genesis(store, genesis)
    rubric = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        asset_proposal_effect(Asset(id="unused", name="rubric", content="rubric")),
        idempotency_key="rubric",
    ).assets[0]
    evidence = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        asset_proposal_effect(Asset(id="unused", name="evidence", content="evidence")),
        idempotency_key="evidence",
    ).assets[0]
    policy = {
        "mode": "independent",
        "policy_version": "cli-review-v1",
        "required_attestations": 1,
        "rubric_asset_ids": [rubric.id],
        "evidence_asset_ids": [evidence.id],
        "verifier_capabilities": ["verify.human"],
        "output_requirements": {
            "final": {"producer_actor_ids": ["human:target-publisher"]}
        },
        "output_shapes": {"final": "nonempty_string"},
    }
    source = build_control_plane_contract(
        name="source",
        description="Create final",
        outputs=("final",),
        budget=5,
        acceptance_policy=policy,
    )
    assert publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        contract_declaration_effect(source),
        idempotency_key="source",
    ).accepted
    target = publish_effect(
        store,
        store,
        genesis,
        target_actor,
        signer,
        asset_proposal_effect(Asset(id="unused", name="final", content='"Result"')),
        idempotency_key="target",
    ).assets[0]

    class NoopSync:
        def sync(self):
            return None

    monkeypatch.setattr(
        aigineering.application, "require_redis", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        aigineering.application, "collaboration_service", lambda _store: NoopSync()
    )
    monkeypatch.setattr(
        runtime_services, "collaboration_service", lambda _store: NoopSync()
    )
    monkeypatch.setattr(cli, "require_redis", lambda: None)
    monkeypatch.setattr(cli, "collaboration_service", lambda _store: NoopSync())
    monkeypatch.setattr(service, "require_redis", lambda: None)
    monkeypatch.setattr(service, "sync_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli, "_persistent_snapshot", lambda: SQLiteStore.snapshot_from_path(database)
    )
    monkeypatch.setattr(cli, "_persistent_store", lambda: SQLiteStore(database))

    def publisher_for(actual_store, _plugin, _caps):
        return CandidatePublisher(actual_store, actual_store, genesis, operator, signer)

    monkeypatch.setattr(service, "ensure_local_plugin_publisher", publisher_for)
    return store, database, genesis, signer, operator, report_actor, source, target


def test_cli_request_list_and_decide_use_relation_asset_ids(tmp_path, monkeypatch):
    store, _database, genesis, signer, operator, report_actor, source, target = _setup(
        tmp_path, monkeypatch
    )
    runner = CliRunner()
    requested = runner.invoke(
        _app(),
        [
            "task",
            "request-review",
            source.id,
            "--output",
            "final",
            "--asset",
            target.id,
            "--reason",
            "Check the result",
            "--idempotency-key",
            "request-cli",
            "--json",
        ],
    )
    assert requested.exit_code == 0, requested.output
    request = json.loads(requested.output)["data"]
    request_id = request["asset_id"]
    assert request["relation"]["relation"] == "task-review-request"
    assert f"aig task review decide {request_id}" in requested.output

    history = runner.invoke(_app(), ["task", "reviews", source.id, "--json"])
    assert history.exit_code == 0, history.output
    listed = json.loads(history.output)["data"]
    assert len(listed) == 1 and listed[0]["asset_id"] == request_id
    assert "record_type" not in listed[0]
    report = publish_effect(
        store,
        store,
        genesis,
        report_actor,
        signer,
        asset_proposal_effect(
            Asset(
                id="unused",
                name=request["relation"]["report_output"],
                content="Reviewed result",
            )
        ),
        idempotency_key="report",
    ).assets[0]
    decided = runner.invoke(
        _app(),
        [
            "task",
            "review",
            "decide",
            request_id,
            "--report",
            report.id,
            "--verdict",
            "accept",
            "--reason",
            "The evidence supports it",
            "--json",
        ],
    )
    assert decided.exit_code == 0, decided.output
    decision = json.loads(decided.output)["data"]
    assert decision["relation"]["relation"] == "task-review-decision"
    assert decision["relation"]["request_asset_id"] == request_id
    assert len(store.scan_runtime_records(record_type="output.qualified")) == 1
    receipts = [
        record
        for _revision, record in store.scan_runtime_records(
            record_type="candidate.received"
        )
        if tuple(record.payload.get("effect_types", ()))
        in {
            ("contract.declare", "asset.propose"),
            ("asset.attest", "asset.propose"),
        }
    ]
    assert len(receipts) == 2


def test_cli_rejects_wrong_output_role_without_review_publication(
    tmp_path, monkeypatch
):
    store, _database, _genesis, _signer, _operator, _report_actor, source, target = (
        _setup(tmp_path, monkeypatch)
    )
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "request-review",
            source.id,
            "--output",
            "wrong",
            "--asset",
            target.id,
            "--reason",
            "Review",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert not any(
        record.record_type == "candidate.received"
        and tuple(record.payload.get("effect_types", ()))
        == ("contract.declare", "asset.propose")
        for _revision, record in store.scan_runtime_records()
    )


def test_review_decide_help_describes_asset_and_attestation_method():
    result = CliRunner().invoke(_app(), ["task", "review", "decide", "--help"])
    assert result.exit_code == 0
    assert "ordinary decision Asset" in result.output
    assert "core attestation" in result.output
