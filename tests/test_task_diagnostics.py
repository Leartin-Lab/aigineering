"""Fact-derived task attempt history and privacy regressions."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from click import Group
from click.testing import CliRunner

from aigineering.agent.harness import HarnessCandidateAdapter
from aigineering.cli.task_diagnostics import register_task_diagnostic_commands
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract, hash_contract_v3
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import create_entry, trace_record_from_entry
from aigineering.core.worker_coordination import authenticate_worker_command
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.local_identity import ensure_local_domain
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    contract_cancellation_effect,
    contract_declaration_effect,
    worker_registration_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract
from aigineering.runtime import claim_next_package, submit_worker_proposal
from aigineering.redis_collaboration import RedisCollaborationUnavailable
from aigineering.task_diagnostics import (
    TaskDiagnosticError,
    _release_terminal_is_valid,
    _validate_grant_provenance,
    task_diagnostics,
    task_invocation_failure_binding,
    task_runs,
)


def _workspace(tmp_path: Path, monkeypatch) -> tuple[Path, str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "task-diagnostics.sqlite"
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / "identity" / "root.ed25519")
    )
    store = SQLiteStore(database)
    ensure_local_domain(store)
    fields = {
        "name": "diagnostic task",
        "description": "A local task used to inspect claims.",
        "inputs": [],
        "outputs": [],
        "activation": "",
        "budget": 3,
        "tool_scope": [],
        "labels": [],
        "origin": "human",
    }
    contract = Contract(
        id=hash_contract(**fields),
        name=fields["name"],
        description=fields["description"],
        budget=3,
    )
    store.add_contract(contract)
    store.register_worker(WorkerRegistration("worker:diagnostics"))
    claim = store.claim_contract(
        contract.id,
        "worker:diagnostics",
        package_id="pkg:" + "f" * 64,
    )
    assert claim is not None
    for _ in range(3):
        claim = store.renew_claim(
            claim["claim_id"], claim["epoch"], claim["worker_id"], lease_seconds=3600
        )
        assert claim is not None
    store.append_runtime_record(
        create_runtime_record(
            "candidate.authentication_rejected",
            {
                "claimed_candidate_id": "untrusted-id-collision",
                "diagnostic": "PRIVATE_AUTH_DIAGNOSTIC_MARKER",
            },
        )
    )
    claim_id = str(claim["claim_id"])
    store.close()
    return database, contract.id, claim_id


def _provenance_chain(
    *,
    request_target=None,
    include_target: bool = True,
    malformed: str | None = None,
):
    worker_id = "worker:provenance-test"
    key_id = "worker-key"
    candidate_id = "candidate:v1:" + "a" * 64
    receipt_payload = {
        "actor_id": worker_id,
        "candidate_id": candidate_id,
        "domain_id": "domain:provenance-test",
        "effect_types": ["worker.claim"],
        "key_id": key_id,
        "metadata": {},
        "signature": "signature",
        "signature_kind": "ed25519",
    }
    if malformed == "receipt_actor":
        receipt_payload["actor_id"] = "worker:other"
    elif malformed == "receipt_candidate":
        receipt_payload["candidate_id"] = "candidate:v1:" + "b" * 64
    elif malformed == "receipt_key":
        receipt_payload["key_id"] = "other-key"
    elif malformed == "receipt_effect":
        receipt_payload["effect_types"] = ["contract.publish"]
    elif malformed == "receipt_effect_none":
        receipt_payload["effect_types"] = None
    elif malformed == "receipt_effect_scalar":
        receipt_payload["effect_types"] = 7
    elif malformed == "receipt_effect_string":
        receipt_payload["effect_types"] = "worker.claim"
    elif malformed == "receipt_effect_mapping":
        receipt_payload["effect_types"] = {"worker.claim": True}
    elif malformed == "receipt_effect_nested":
        receipt_payload["effect_types"] = [["worker.claim"]]
    receipt = create_runtime_record("candidate.received", receipt_payload)
    if malformed == "receipt_identity":
        receipt = replace(receipt, id="record:invalid-receipt-identity")

    request_payload = {
        "candidate_id": candidate_id,
        "idempotency_key": "claim-request-1",
        "key_id": key_id,
        "lease_seconds": 60,
        "worker_id": worker_id,
    }
    if include_target:
        request_payload["contract_id"] = request_target
    if malformed == "request_candidate":
        request_payload["candidate_id"] = "not-a-canonical-candidate"
    elif malformed == "request_key":
        request_payload["key_id"] = []
    elif malformed == "request_extra":
        request_payload["extra"] = "not part of the signed command shape"
    request_parents = [receipt.id]
    if malformed == "request_multiple_parents":
        extra_receipt = create_runtime_record(
            "candidate.received",
            {**receipt_payload, "candidate_id": "candidate:v1:" + "c" * 64},
        )
        request_parents.append(extra_receipt.id)
    request = create_runtime_record(
        "worker.claim.requested",
        request_payload,
        causal_parents=request_parents,
    )
    if malformed == "request_identity":
        request = replace(request, id="record:invalid-request-identity")
    grant = create_runtime_record(
        "claim.granted",
        {
            "claim_id": "lease:" + "d" * 64,
            "contract_id": "contract:target",
            "epoch": 1,
            "lease_until": "2099-01-01T00:00:00Z",
            "package_id": "pkg:" + "e" * 64,
            "worker_id": worker_id,
        },
        causal_parents=(request.id,),
    )
    revisions = {receipt.id: 1, request.id: 2, grant.id: 3}
    if malformed == "request_after_receipt":
        revisions[receipt.id], revisions[request.id] = 2, 1
    by_id = {
        record.id: (revisions[record.id], record)
        for record in (receipt, request, grant)
    }
    if malformed == "request_multiple_parents":
        by_id[extra_receipt.id] = (0, extra_receipt)
    return grant, by_id


@pytest.mark.parametrize(
    ("target", "include_target", "malformed", "expected"),
    [
        (None, True, None, "signed"),
        ("contract:target", True, None, "signed"),
        ("contract:other", True, None, None),
        (True, True, None, None),
        ([], True, None, None),
        (None, False, None, None),
        (None, True, "receipt_actor", None),
        (None, True, "receipt_candidate", None),
        (None, True, "receipt_key", None),
        (None, True, "receipt_effect", None),
        (None, True, "receipt_effect_none", None),
        (None, True, "receipt_effect_scalar", None),
        (None, True, "receipt_effect_string", None),
        (None, True, "receipt_effect_mapping", None),
        (None, True, "receipt_effect_nested", None),
        (None, True, "receipt_identity", None),
        (None, True, "request_identity", None),
        (None, True, "request_candidate", None),
        (None, True, "request_key", None),
        (None, True, "request_extra", None),
        (None, True, "request_multiple_parents", None),
        (None, True, "request_after_receipt", None),
    ],
)
def test_grant_provenance_accepts_only_exact_signed_selector_and_chain(
    target, include_target, malformed, expected
):
    grant, by_id = _provenance_chain(
        request_target=target,
        include_target=include_target,
        malformed=malformed,
    )
    evidence_ids = [grant.id]
    result = _validate_grant_provenance(grant, by_id, evidence_ids)
    assert result == expected
    if expected is None:
        assert evidence_ids == [grant.id]
    else:
        request = by_id[grant.causal_parents[0]][1]
        receipt = by_id[request.causal_parents[0]][1]
        assert set(evidence_ids) == {grant.id, request.id, receipt.id}


def test_runs_and_diagnostics_use_fact_history_and_fixed_cursor(tmp_path, monkeypatch):
    database, contract_id, claim_id = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    try:
        before = store.get_runtime_revision()
        first = task_runs(store, contract_id)
        row = first["data"]["runs"][0]
        grant = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("claim_id") == claim_id
        )
        assert first["ok"] is True
        assert row["status"] == "active"
        assert row["grant_provenance"] == "legacy_recordless"
        assert row["lease_until"] > row["grant_recorded_at"]
        assert row["grant_recorded_at"] == grant.recorded_at
        assert row["fact_ids"]
        assert store.get_runtime_revision() == before

        page = task_diagnostics(store, contract_id, claim_id, limit=1)
        assert page["ok"] is True
        assert page["data"]["truncated"] is True
        observed_at = page["data"]["observed_at"]
        watermark = page["data"]["watermark"]
        assert page["data"]["events"][0]["record_type"] == "claim.granted"
        assert page["data"]["events"][0]["recorded_at"] == grant.recorded_at

        claim = store.get_claim(contract_id)
        assert claim is not None
        store.renew_claim(
            claim["claim_id"], claim["epoch"], claim["worker_id"], lease_seconds=3600
        )
        next_page = task_diagnostics(
            store, contract_id, claim_id, limit=1, cursor=page["data"]["next_cursor"]
        )
        assert next_page["data"]["watermark"] == watermark
        assert next_page["data"]["observed_at"] == observed_at
        assert all(
            event["record_type"] != "candidate.authentication_rejected"
            for event in page["data"]["events"] + next_page["data"]["events"]
        )
        assert "PRIVATE_AUTH_DIAGNOSTIC_MARKER" not in json.dumps(next_page)
        old_page_ids = [
            page["data"]["events"][0]["record_id"],
            next_page["data"]["events"][0]["record_id"],
        ]
        cursor = next_page["data"]["next_cursor"]
        while cursor is not None:
            next_page = task_diagnostics(
                store, contract_id, claim_id, limit=1, cursor=cursor
            )
            assert next_page["data"]["watermark"] == watermark
            assert next_page["data"]["observed_at"] == observed_at
            old_page_ids.extend(
                event["record_id"] for event in next_page["data"]["events"]
            )
            cursor = next_page["data"]["next_cursor"]
        assert len(old_page_ids) == 4
        assert len(set(old_page_ids)) == 4
        foreign_fields = {
            "name": "foreign task",
            "description": "A separate task.",
            "inputs": [],
            "outputs": [],
            "activation": "",
            "budget": 1,
            "tool_scope": [],
            "labels": [],
            "origin": "human",
        }
        store.add_contract(
            Contract(
                id=hash_contract(**foreign_fields),
                name=foreign_fields["name"],
                description=foreign_fields["description"],
                budget=1,
            )
        )
        with pytest.raises(TaskDiagnosticError, match="cursor_scope_mismatch"):
            task_diagnostics(
                store,
                hash_contract(**foreign_fields),
                claim_id,
                cursor=page["data"]["next_cursor"],
            )
        reopened = SQLiteStore(database)
        try:
            baseline = task_diagnostics(reopened, contract_id, claim_id)
            reopened.rebuild_claim_projection()
            rebuilt = task_diagnostics(reopened, contract_id, claim_id)
            assert baseline["data"]["events"] == rebuilt["data"]["events"]
            assert baseline["data"]["integrity"] == rebuilt["data"]["integrity"]
        finally:
            reopened.close()
    finally:
        store.close()


@pytest.mark.parametrize(
    "effect_types",
    [None, 7, "worker.claim", {"worker.claim": True}, [["worker.claim"]]],
)
def test_runs_classifies_malformed_claim_receipt_effect_types_safely(
    tmp_path, monkeypatch, effect_types
):
    database, contract_id, _claim_id = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    try:
        existing = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("contract_id") == contract_id
        )
        worker_id = existing.payload["worker_id"]
        candidate_id = "candidate:v1:" + "f" * 64
        receipt = create_runtime_record(
            "candidate.received",
            {
                "actor_id": worker_id,
                "candidate_id": candidate_id,
                "domain_id": "domain:provenance-test",
                "effect_types": effect_types,
                "key_id": "worker-key",
                "metadata": {},
                "signature": "fixture-signature",
                "signature_kind": "ed25519",
            },
        )
        request = create_runtime_record(
            "worker.claim.requested",
            {
                "candidate_id": candidate_id,
                "contract_id": None,
                "idempotency_key": "malformed-effect-types-claim",
                "key_id": "worker-key",
                "lease_seconds": 60,
                "worker_id": worker_id,
            },
            causal_parents=(receipt.id,),
        )
        grant = create_runtime_record(
            "claim.granted",
            {
                "claim_id": "lease:" + "f" * 64,
                "contract_id": contract_id,
                "epoch": 77,
                "lease_until": "2099-01-01T00:00:00Z",
                "package_id": "pkg:" + "f" * 64,
                "worker_id": worker_id,
            },
            causal_parents=(request.id,),
        )
        for record in (receipt, request, grant):
            store.append_runtime_record(record)

        result = task_runs(store, contract_id)
        row = next(
            item
            for item in result["data"]["runs"]
            if item.get("claim_id") == "lease:" + "f" * 64
        )
        assert result["ok"] is False
        assert row["status"] == "integrity_error"
        assert {issue["code"] for issue in row["integrity"]["issues"]} >= {
            "grant_provenance_invalid"
        }
    finally:
        store.close()


@pytest.mark.parametrize("committed_ids", [None, 7, {"record:example": True}])
def test_release_terminal_rejects_malformed_committed_member_container(committed_ids):
    candidate_id = "candidate:v1:" + "a" * 64
    source = create_runtime_record(
        "candidate.received",
        {
            "actor_id": "human:owner",
            "candidate_id": candidate_id,
            "effect_types": ["contract.cancel"],
        },
    )
    terminal = create_runtime_record(
        "lifecycle.terminal",
        {
            "actor_id": "human:owner",
            "contract_id": "contract:target",
            "reason": "operator cancellation",
            "terminal": "cancelled",
        },
        causal_parents=(source.id,),
    )
    head = create_runtime_record(
        "candidate.committed",
        {"candidate_id": candidate_id, "committed_record_ids": committed_ids},
        causal_parents=(source.id,),
    )
    indexed = [(1, source), (2, terminal), (3, head)]
    by_id = {record.id: (revision, record) for revision, record in indexed}
    assert (
        _release_terminal_is_valid(terminal, "contract:target", by_id, indexed) is False
    )


@pytest.mark.parametrize("terminal_value", [{"malformed": True}, [], 7, None])
def test_release_terminal_rejects_non_string_terminal_without_crashing(
    terminal_value,
):
    terminal = create_runtime_record(
        "lifecycle.terminal",
        {"contract_id": "contract:target", "terminal": terminal_value},
    )
    assert _release_terminal_is_valid(terminal, "contract:target", {}, []) is False


@pytest.mark.parametrize("terminal_value", [{"malformed": True}, [], 7, None])
def test_diagnostics_classifies_malformed_terminal_state_safely(
    tmp_path, monkeypatch, terminal_value
):
    database, contract_id, claim_id = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    try:
        grant = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("claim_id") == claim_id
        )
        malformed_terminal = create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": contract_id, "terminal": terminal_value},
            causal_parents=(grant.id,),
        )
        watermark = store.get_runtime_revision()
        existing_records = store.scan_runtime_records(through_revision=watermark)

        def scan_with_malformed_terminal(*, through_revision=None, record_type=None):
            rows = [*existing_records, (watermark + 1, malformed_terminal)]
            if through_revision is not None:
                rows = [row for row in rows if row[0] <= through_revision]
            if record_type is not None:
                rows = [row for row in rows if row[1].record_type == record_type]
            return rows

        monkeypatch.setattr(store, "get_runtime_revision", lambda: watermark + 1)
        monkeypatch.setattr(store, "scan_runtime_records", scan_with_malformed_terminal)
        result = task_diagnostics(store, contract_id, claim_id)
        assert result["ok"] is False
        assert any(
            issue["code"] == "terminal_causality_invalid"
            for issue in result["data"]["integrity"]["issues"]
        )
    finally:
        store.close()


def test_cli_registers_read_only_commands_and_reports_integrity(tmp_path, monkeypatch):
    database, contract_id, claim_id = _workspace(tmp_path, monkeypatch)

    class Collaboration:
        def sync(self):
            return {"ok": True}

    monkeypatch.setattr(
        "aigineering.cli.task_diagnostics._persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_diagnostics.collaboration_service",
        lambda _store: Collaboration(),
    )
    root = Group()
    task = Group("task")
    register_task_diagnostic_commands(task)
    root.add_command(task)

    result = CliRunner().invoke(root, ["task", "runs", contract_id, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["runs"][0]["status"] == "active"

    result = CliRunner().invoke(
        root,
        ["task", "diagnostics", contract_id, "--claim", claim_id, "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["events"]
    assert "PRIVATE_AUTH_DIAGNOSTIC_MARKER" not in result.output

    class UnavailableCollaboration:
        def sync(self):
            raise RedisCollaborationUnavailable("private connection detail")

    monkeypatch.setattr(
        "aigineering.cli.task_diagnostics.collaboration_service",
        lambda _store: UnavailableCollaboration(),
    )
    unavailable = CliRunner().invoke(root, ["task", "runs", contract_id, "--json"])
    assert unavailable.exit_code == 1
    error = json.loads(unavailable.output)
    assert error == {
        "ok": False,
        "error": "redis_unavailable",
        "error_type": "RedisCollaborationUnavailable",
    }
    assert "private connection detail" not in unavailable.output


def test_invocation_failure_exports_only_allowlisted_usage_and_category(
    tmp_path, monkeypatch
):
    database, contract_id, claim_id = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    markers = {
        "raw_output": "PRIVATE_RAW_OUTPUT_MARKER",
        "diagnostic": "PRIVATE_DIAGNOSTIC_MARKER",
        "model": "PRIVATE_MODEL_MARKER",
        "provider": "PRIVATE_PROVIDER_MARKER",
        "worker_profile": "PRIVATE_PROFILE_MARKER",
    }
    try:
        claim = store.get_claim(contract_id)
        assert claim is not None
        failure = create_runtime_record(
            "worker.invocation_failed",
            {
                "claim_id": claim_id,
                "contract_id": contract_id,
                "package_id": claim["package_id"],
                "worker_id": claim["worker_id"],
                "category": "unrecognized:" + markers["diagnostic"],
                "retryable": True,
                "status_code": 502,
                "raw_output": markers["raw_output"],
                "diagnostic": markers["diagnostic"],
                "usage_metadata": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                    "model": markers["model"],
                    "provider": markers["provider"],
                    "worker_profile": markers["worker_profile"],
                },
            },
        )
        terminal = create_terminal_record(
            contract_id, "failed", causal_parents=(failure.id,)
        )
        entry = create_entry(
            contract_id,
            "worker_invocation_failed",
            worker_id=claim["worker_id"],
            rejected_fragments=["private trace material"],
            authority_result="rejected",
        )
        trace_fact = trace_record_from_entry(entry, causal_parents=(failure.id,))
        store.commit_worker_invocation_failure(
            trace_entry=entry,
            runtime_records=(failure, terminal, trace_fact),
            claim_id=claim_id,
            worker_id=claim["worker_id"],
            package_id=claim["package_id"],
            claim_epoch=claim["epoch"],
        )
        result = task_diagnostics(store, contract_id, claim_id)
        assert result["ok"] is True, (
            result["data"]["integrity"],
            [
                (record.id, record.record_type, dict(record.payload))
                for _, record in store.scan_runtime_records()
                if record.payload.get("claim_id") == claim_id
            ],
        )
        row = task_runs(store, contract_id)["data"]["runs"][0]
        assert row["status"] == "invocation_failed"
        assert row["failure"]["category"] == "unknown"
        assert row["failure"]["usage"] == {
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "total_tokens": 6,
        }
        binding = task_invocation_failure_binding(
            store,
            contract_id,
            claim_id,
            claim["package_id"],
            claim["worker_id"],
            claim["epoch"],
        )
        import aigineering.task_diagnostics as diagnostics_module
        from aigineering.task_view_query import load_task_view_query_context

        original_build_runs = diagnostics_module._build_runs
        build_calls = 0

        def counted_build_runs(*args, **kwargs):
            nonlocal build_calls
            build_calls += 1
            return original_build_runs(*args, **kwargs)

        monkeypatch.setattr(diagnostics_module, "_build_runs", counted_build_runs)
        query = load_task_view_query_context(store)
        proof_cache = {}
        for _ in range(2):
            task_invocation_failure_binding(
                store,
                contract_id,
                claim_id,
                claim["package_id"],
                claim["worker_id"],
                claim["epoch"],
                query_context=query,
                _proof_cache=proof_cache,
                _observed_at="fixed-observation",
            )
        assert build_calls == 1
        task_invocation_failure_binding(
            store,
            contract_id,
            claim_id,
            claim["package_id"],
            claim["worker_id"],
            claim["epoch"],
            query_context=query,
            _proof_cache=proof_cache,
            _observed_at="different-observation",
        )
        assert build_calls == 2
        fresh_query = load_task_view_query_context(store)
        task_invocation_failure_binding(
            store,
            contract_id,
            claim_id,
            claim["package_id"],
            claim["worker_id"],
            claim["epoch"],
            query_context=fresh_query,
            _proof_cache=proof_cache,
            _observed_at="fixed-observation",
        )
        assert build_calls == 3
        for _ in range(2):
            task_invocation_failure_binding(
                store,
                contract_id,
                claim_id,
                claim["package_id"],
                claim["worker_id"],
                claim["epoch"],
                _proof_cache=proof_cache,
            )
        assert build_calls == 5
        assert binding == {
            "kind": "invocation_failure",
            "status": "invocation_failed",
            "contract_id": contract_id,
            "domain_id": result["data"]["domain_id"],
            "claim_id": claim_id,
            "package_id": claim["package_id"],
            "worker_id": claim["worker_id"],
            "epoch": claim["epoch"],
            "grant_record_id": row["grant_record_id"],
            "failure_record_id": row["failure"]["record_id"],
            "release_record_id": next(
                fact_id
                for fact_id in row["fact_ids"]
                if store.get_runtime_record(fact_id).record_type == "claim.released"
            ),
            "watermark": result["revision"],
            "category": "unknown",
            "retryable": True,
            "status_code": 502,
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "total_tokens": 6,
            },
        }
        binding_json = json.dumps(binding)
        for marker in markers.values():
            assert marker not in binding_json
        for wrong_claim, wrong_package, wrong_worker, wrong_epoch in (
            (claim_id, "pkg:" + "0" * 64, claim["worker_id"], claim["epoch"]),
            (claim_id, claim["package_id"], "worker:other", claim["epoch"]),
            (claim_id, claim["package_id"], claim["worker_id"], claim["epoch"] + 1),
            (
                "lease:" + "0" * 64,
                claim["package_id"],
                claim["worker_id"],
                claim["epoch"],
            ),
        ):
            with pytest.raises(TaskDiagnosticError):
                task_invocation_failure_binding(
                    store,
                    contract_id,
                    wrong_claim,
                    wrong_package,
                    wrong_worker,
                    wrong_epoch,
                )
        serialized = json.dumps(result)
        for marker in markers.values():
            assert marker not in serialized

        class Collaboration:
            def sync(self):
                return {"ok": True}

        monkeypatch.setattr(
            "aigineering.cli.task_diagnostics._persistent_snapshot",
            lambda: SQLiteStore.snapshot_from_path(database),
        )
        monkeypatch.setattr(
            "aigineering.cli.task_diagnostics.collaboration_service",
            lambda _store: Collaboration(),
        )
        root = Group()
        task = Group("task")
        register_task_diagnostic_commands(task)
        root.add_command(task)
        for output_args in (["--json"], []):
            cli_result = CliRunner().invoke(
                root,
                [
                    "task",
                    "diagnostics",
                    contract_id,
                    "--claim",
                    claim_id,
                    *output_args,
                ],
            )
            assert cli_result.exit_code == 0, cli_result.output
            for marker in markers.values():
                assert marker not in cli_result.output

        duplicate_failure = dict(failure.payload)
        duplicate_failure["status_code"] = failure.payload["status_code"] + 1
        store.append_runtime_record(
            create_runtime_record("worker.invocation_failed", duplicate_failure)
        )
        with pytest.raises(TaskDiagnosticError, match="claim_integrity_error"):
            task_invocation_failure_binding(
                store,
                contract_id,
                claim_id,
                claim["package_id"],
                claim["worker_id"],
                claim["epoch"],
            )
    finally:
        store.close()


@pytest.mark.parametrize("domain_wide", [False, True])
def test_signed_attempt_uses_exact_receipt_and_ignores_claimed_id_collision(
    domain_wide,
):
    store = SQLiteStore(":memory:")
    signer = Ed25519Signer()
    actor = ActorKey(
        "worker:diagnostic-test",
        "worker-key",
        signer.kind,
        signer.signer_id,
        (
            "contract.publish",
            "contract.cancel",
            "worker.register",
            "worker.submit",
        ),
    )
    genesis = create_genesis_manifest(
        "diagnostic-signed-test", (actor,), "policy:diagnostic-signed-test"
    )
    initialize_genesis(store, genesis)
    publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        worker_registration_effect(
            WorkerRegistration(
                actor.actor_id,
                actor_id=actor.actor_id,
                key_id=actor.key_id,
            )
        ),
        idempotency_key="register-diagnostic-worker",
    )
    fields = {
        "name": "signed diagnostic task",
        "description": "Publish one signed output.",
        "inputs": (),
        "outputs": ("verified_result",),
        "activation": "",
        "budget": 3,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    contract = Contract(id=hash_contract_v3(**fields), **fields)
    publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(contract),
        idempotency_key="publish-diagnostic-task",
    )
    adapter = HarnessCandidateAdapter(genesis.id, actor, signer)
    claim_candidate = adapter.claim_candidate(
        request_id=f"diagnostic-claim-{domain_wide}",
        contract_id=None if domain_wide else contract.id,
    )
    command = authenticate_worker_command(claim_candidate, "worker.claim", store)
    claimed = claim_next_package(
        store,
        worker_id=adapter.worker_id,
        contract_id=contract.id,
        claim_runtime_records=command.runtime_records,
        claim_candidate=command.candidate,
    )
    assert claimed is not None
    proposal = adapter.result_candidate(
        claimed.package, '/exec {"outputs":{"verified_result":"ok"}}'
    )
    submission = submit_worker_proposal(proposal, store)
    assert submission["status"] == "accepted"
    fake_auth = create_runtime_record(
        "candidate.authentication_rejected",
        {
            "claimed_candidate_id": proposal.id,
            "diagnostic": "PRIVATE_UNAUTHENTICATED_COLLISION",
        },
    )
    store.append_runtime_record(fake_auth)
    try:
        runs = task_runs(store, contract.id)
        assert runs["ok"] is True
        row = runs["data"]["runs"][0]
        assert row["status"] == "submitted"
        assert row["grant_provenance"] == "signed"
        assert row["candidate_id"] == proposal.id
        assert row["attempt_outcome"] == "output_asserted"
        assert fake_auth.id not in row["fact_ids"]
        diagnostics = task_diagnostics(store, contract.id, claimed.package.claim_id)
        assert diagnostics["ok"] is True
        assert fake_auth.id not in {
            event["record_id"] for event in diagnostics["data"]["events"]
        }
        assert "PRIVATE_UNAUTHENTICATED_COLLISION" not in json.dumps(diagnostics)

        cancel_fields = {
            **fields,
            "name": "signed cancellation releases active claim",
            "description": "A signed cancellation exercises terminal-backed release.",
            "outputs": (),
        }
        cancel_contract = Contract(
            id=hash_contract_v3(**cancel_fields), **cancel_fields
        )
        publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            contract_declaration_effect(cancel_contract),
            idempotency_key="publish-cancellation-task",
        )
        cancel_claim_candidate = adapter.claim_candidate(
            request_id="diagnostic-cancel-claim", contract_id=cancel_contract.id
        )
        cancel_command = authenticate_worker_command(
            cancel_claim_candidate, "worker.claim", store
        )
        cancel_claim = claim_next_package(
            store,
            worker_id=adapter.worker_id,
            contract_id=cancel_contract.id,
            claim_runtime_records=cancel_command.runtime_records,
            claim_candidate=cancel_command.candidate,
        )
        assert cancel_claim is not None
        publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            contract_cancellation_effect(cancel_contract.id, "signed cancellation"),
            idempotency_key="cancel-diagnostic-task",
        )
        cancelled_runs = task_runs(store, cancel_contract.id)
        cancelled_row = cancelled_runs["data"]["runs"][0]
        assert cancelled_runs["ok"] is True
        assert cancelled_row["status"] == "released"
        cancelled_diagnostics = task_diagnostics(
            store, cancel_contract.id, cancel_claim.package.claim_id
        )
        assert cancelled_diagnostics["ok"] is True
        assert "lifecycle.terminal" in {
            event["record_type"] for event in cancelled_diagnostics["data"]["events"]
        }

    finally:
        store.close()


def test_durable_expiry_and_conflicting_history_are_distinct(tmp_path, monkeypatch):
    database, contract_id, claim_id = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    try:
        grant = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("claim_id") == claim_id
        )
        latest_claim = store.get_claim(contract_id)
        assert latest_claim is not None
        expired = create_runtime_record(
            "claim.expired",
            {
                "claim_id": claim_id,
                "contract_id": contract_id,
                "epoch": grant.payload["epoch"],
                "lease_until": latest_claim["lease_until"],
                "package_id": grant.payload["package_id"],
                "worker_id": grant.payload["worker_id"],
            },
            causal_parents=(grant.id,),
        )
        terminal = create_terminal_record(
            contract_id, "failed", causal_parents=(expired.id,)
        )
        entry = create_entry(
            contract_id,
            "claim_expired",
            worker_id=grant.payload["worker_id"],
            authority_result="rejected",
        )
        trace_fact = trace_record_from_entry(entry, causal_parents=(expired.id,))
        store.commit_claim_expiration(
            trace_entry=entry,
            runtime_records=(expired, terminal, trace_fact),
            claim_id=claim_id,
            claim_epoch=grant.payload["epoch"],
            expected_lease_until=latest_claim["lease_until"],
            observed_at=expired.recorded_at,
        )
        result = task_runs(store, contract_id)
        assert result["ok"] is True, result["data"]["integrity"]
        assert result["data"]["runs"][0]["status"] == "expired"
        diagnostics = task_diagnostics(store, contract_id, claim_id)
        assert diagnostics["ok"] is True
        assert "lifecycle.terminal" in {
            event["record_type"] for event in diagnostics["data"]["events"]
        }

        orphan_closure = create_runtime_record(
            "attempt.closed",
            {
                "candidate_id": "candidate:orphan",
                "claim_id": claim_id,
                "contract_id": contract_id,
                "outcome": "failed",
            },
        )
        store.append_runtime_record(orphan_closure)
        orphaned = task_runs(store, contract_id)
        assert orphaned["ok"] is False
        assert "orphan_attempt_closure" in {
            issue["code"]
            for issue in orphaned["data"]["runs"][0]["integrity"]["issues"]
        }

        release = create_runtime_record(
            "claim.released",
            {
                "claim_id": claim_id,
                "contract_id": contract_id,
                "epoch": grant.payload["epoch"],
                "package_id": grant.payload["package_id"],
                "worker_id": grant.payload["worker_id"],
            },
            causal_parents=(grant.id,),
        )
        store.append_runtime_record(release)
        conflicted = task_runs(store, contract_id)
        assert conflicted["ok"] is False
        assert conflicted["data"]["runs"][0]["status"] == "integrity_error"
        assert "conflicting_claim_transitions" in {
            issue["code"]
            for issue in conflicted["data"]["runs"][0]["integrity"]["issues"]
        }
    finally:
        store.close()


@pytest.mark.parametrize("domain_wide", [False, True])
def test_signed_rejection_uses_exact_failed_attempt_chain(domain_wide):
    store = SQLiteStore(":memory:")
    signer = Ed25519Signer()
    actor = ActorKey(
        "worker:diagnostic-rejection",
        "worker-key",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "worker.register", "worker.submit"),
    )
    genesis = create_genesis_manifest(
        "diagnostic-rejection-test", (actor,), "policy:diagnostic-rejection-test"
    )
    initialize_genesis(store, genesis)
    publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        worker_registration_effect(
            WorkerRegistration(
                actor.actor_id, actor_id=actor.actor_id, key_id=actor.key_id
            )
        ),
        idempotency_key="register-diagnostic-rejection-worker",
    )
    fields = {
        "name": "rejected diagnostic task",
        "description": "A signed output outside the declared output set is rejected.",
        "inputs": (),
        "outputs": ("declared_result",),
        "activation": "",
        "budget": 3,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    contract = Contract(id=hash_contract_v3(**fields), **fields)
    publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(contract),
        idempotency_key="publish-rejected-diagnostic-task",
    )
    adapter = HarnessCandidateAdapter(genesis.id, actor, signer)
    request = adapter.claim_candidate(
        request_id=f"rejected-diagnostic-claim-{domain_wide}",
        contract_id=None if domain_wide else contract.id,
    )
    command = authenticate_worker_command(request, "worker.claim", store)
    claimed = claim_next_package(
        store,
        worker_id=adapter.worker_id,
        contract_id=contract.id,
        claim_runtime_records=command.runtime_records,
        claim_candidate=command.candidate,
    )
    assert claimed is not None
    proposal = adapter.result_candidate(
        claimed.package, '/exec {"outputs":{"wrong_result":"no"}}'
    )
    result = submit_worker_proposal(proposal, store)
    assert result["status"] == "rejected"
    try:
        run = task_runs(store, contract.id)
        assert run["ok"] is True
        row = run["data"]["runs"][0]
        assert row["status"] == "rejected"
        assert row["grant_provenance"] == "signed"
        assert row["candidate_id"] == proposal.id
        assert row["attempt_outcome"] == "rejected"
        diagnostics = task_diagnostics(store, contract.id, claimed.package.claim_id)
        assert diagnostics["ok"] is True
        event_types = {event["record_type"] for event in diagnostics["data"]["events"]}
        assert {
            "candidate.received",
            "candidate.rejected",
            "attempt.closed",
            "claim.submitted",
        } <= event_types
    finally:
        store.close()


def test_cursor_is_bound_to_domain_and_rejects_tampering(tmp_path, monkeypatch):
    first_db, contract_id, claim_id = _workspace(tmp_path / "first", monkeypatch)
    first_store = SQLiteStore(first_db)
    second_db, second_contract_id, second_claim_id = _workspace(
        tmp_path / "second", monkeypatch
    )
    second_store = SQLiteStore(second_db)
    try:
        first_page = task_diagnostics(first_store, contract_id, claim_id, limit=1)
        assert first_page["data"]["next_cursor"] is not None
        with pytest.raises(TaskDiagnosticError, match="invalid_cursor"):
            task_diagnostics(
                first_store,
                contract_id,
                claim_id,
                limit=1,
                cursor=first_page["data"]["next_cursor"] + "x",
            )
        assert second_contract_id == contract_id
        with pytest.raises(TaskDiagnosticError, match="cursor_scope_mismatch"):
            task_diagnostics(
                second_store,
                second_contract_id,
                second_claim_id,
                limit=1,
                cursor=first_page["data"]["next_cursor"],
            )
    finally:
        first_store.close()
        second_store.close()


def test_malformed_claim_identity_and_lease_are_never_echoed(tmp_path, monkeypatch):
    database, contract_id, _claim_id = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    marker = "PRIVATE_MALFORMED_CLAIM_MARKER"
    try:
        store.append_runtime_record(
            create_runtime_record(
                "claim.granted",
                {
                    "claim_id": [marker],
                    "contract_id": contract_id,
                    "epoch": 2,
                    "lease_until": marker,
                    "package_id": marker,
                    "worker_id": marker,
                },
            )
        )
        undeclared_worker = "PRIVATE_UNDECLARED_WORKER_MARKER"
        malformed_package = "PRIVATE_MALFORMED_PACKAGE_MARKER"
        store.append_runtime_record(
            create_runtime_record(
                "claim.granted",
                {
                    "claim_id": "lease:" + "a" * 64,
                    "contract_id": contract_id,
                    "epoch": 99,
                    "lease_until": "2099-01-01T00:00:00Z",
                    "package_id": malformed_package,
                    "worker_id": undeclared_worker,
                },
            )
        )
        result = task_runs(store, contract_id)
        malformed = next(
            row
            for row in result["data"]["runs"]
            if row["status"] == "integrity_error" and row["claim_id"] is None
        )
        assert result["ok"] is False
        assert malformed["claim_id"] is None
        assert malformed.get("lease_until") is None
        assert marker not in json.dumps(result)
        undeclared = next(
            row
            for row in result["data"]["runs"]
            if row["claim_id"] == "lease:" + "a" * 64
        )
        assert undeclared["worker_id"] is None
        assert undeclared["package_id"] is None
        assert undeclared_worker not in json.dumps(result)
        assert malformed_package not in json.dumps(result)

        class Collaboration:
            def sync(self):
                return {"ok": True}

        monkeypatch.setattr(
            "aigineering.cli.task_diagnostics._persistent_snapshot",
            lambda: SQLiteStore.snapshot_from_path(database),
        )
        monkeypatch.setattr(
            "aigineering.cli.task_diagnostics.collaboration_service",
            lambda _store: Collaboration(),
        )
        root = Group()
        task = Group("task")
        register_task_diagnostic_commands(task)
        root.add_command(task)
        for output_args in (["--json"], []):
            cli_result = CliRunner().invoke(
                root, ["task", "runs", contract_id, *output_args]
            )
            assert cli_result.exit_code == 1
            assert marker not in cli_result.output
            assert undeclared_worker not in cli_result.output
            assert malformed_package not in cli_result.output
    finally:
        store.close()


def test_malformed_candidate_id_is_never_echoed(tmp_path, monkeypatch):
    database, contract_id, claim_id = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    marker = "PRIVATE_MALFORMED_CANDIDATE_ID_MARKER"
    timestamp_marker = "PRIVATE_MALFORMED_RECORDED_AT_MARKER"
    try:
        grant = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("claim_id") == claim_id
        )
        store.append_runtime_record(
            create_runtime_record(
                "claim.submitted",
                {
                    "candidate_id": marker,
                    "claim_id": claim_id,
                    "contract_id": contract_id,
                    "epoch": grant.payload["epoch"],
                    "package_id": grant.payload["package_id"],
                    "worker_id": grant.payload["worker_id"],
                },
                causal_parents=(grant.id,),
                recorded_at=timestamp_marker,
            )
        )
        malformed_time_claim_id = "lease:" + "b" * 64
        store.append_runtime_record(
            create_runtime_record(
                "claim.granted",
                {
                    "claim_id": malformed_time_claim_id,
                    "contract_id": contract_id,
                    "epoch": 99,
                    "lease_until": "2099-01-01T00:00:00Z",
                    "package_id": "",
                    "worker_id": "worker:diagnostics",
                },
                recorded_at=timestamp_marker,
            )
        )
        runs = task_runs(store, contract_id)
        assert runs["ok"] is False
        row = next(
            item for item in runs["data"]["runs"] if item["claim_id"] == claim_id
        )
        assert row["candidate_id"] is None
        malformed_time_row = next(
            item
            for item in runs["data"]["runs"]
            if item["claim_id"] == malformed_time_claim_id
        )
        assert malformed_time_row["grant_recorded_at"] is None
        assert marker not in json.dumps(runs)
        assert timestamp_marker not in json.dumps(runs)
        diagnostics = task_diagnostics(store, contract_id, claim_id)
        assert diagnostics["ok"] is False
        assert marker not in json.dumps(diagnostics)
        assert timestamp_marker not in json.dumps(diagnostics)
        submitted_event = next(
            event
            for event in diagnostics["data"]["events"]
            if event["record_type"] == "claim.submitted"
        )
        assert submitted_event["recorded_at"] is None

        class Collaboration:
            def sync(self):
                return {"ok": True}

        monkeypatch.setattr(
            "aigineering.cli.task_diagnostics._persistent_snapshot",
            lambda: SQLiteStore.snapshot_from_path(database),
        )
        monkeypatch.setattr(
            "aigineering.cli.task_diagnostics.collaboration_service",
            lambda _store: Collaboration(),
        )
        root = Group()
        task = Group("task")
        register_task_diagnostic_commands(task)
        root.add_command(task)
        for args in (
            ["task", "runs", contract_id, "--json"],
            ["task", "runs", contract_id],
            ["task", "diagnostics", contract_id, "--claim", claim_id, "--json"],
            ["task", "diagnostics", contract_id, "--claim", claim_id],
        ):
            result = CliRunner().invoke(root, args)
            assert result.exit_code == 1
            assert marker not in result.output
            assert timestamp_marker not in result.output
    finally:
        store.close()


def test_release_terminal_witness_must_match_claim_contract(tmp_path, monkeypatch):
    normal_db, normal_contract_id, normal_claim_id = _workspace(
        tmp_path / "normal", monkeypatch
    )
    normal_store = SQLiteStore(normal_db)
    try:
        terminal = create_terminal_record(normal_contract_id, "cancelled")
        normal_store.append_runtime_record(terminal)
        runs = task_runs(normal_store, normal_contract_id)
        row = next(
            item for item in runs["data"]["runs"] if item["claim_id"] == normal_claim_id
        )
        assert runs["ok"] is True
        assert row["status"] == "released"
        diagnostics = task_diagnostics(
            normal_store, normal_contract_id, normal_claim_id
        )
        assert diagnostics["ok"] is True
        assert terminal.id in {
            event["record_id"] for event in diagnostics["data"]["events"]
        }
    finally:
        normal_store.close()

    foreign_db, contract_id, claim_id = _workspace(tmp_path / "foreign", monkeypatch)
    foreign_store = SQLiteStore(foreign_db)
    try:
        grant = next(
            record
            for _, record in foreign_store.scan_runtime_records(
                record_type="claim.granted"
            )
            if record.payload.get("claim_id") == claim_id
        )
        fields = {
            "name": "foreign terminal target",
            "description": "A different Contract for a release-causality regression.",
            "inputs": [],
            "outputs": [],
            "activation": "",
            "budget": 1,
            "tool_scope": [],
            "labels": [],
            "origin": "human",
        }
        foreign_contract = Contract(
            id=hash_contract(**fields),
            name=fields["name"],
            description=fields["description"],
            budget=1,
        )
        foreign_store.add_contract(foreign_contract)
        terminal = create_terminal_record(foreign_contract.id, "cancelled")
        foreign_store.append_runtime_record(terminal)
        release = create_runtime_record(
            "claim.released",
            {
                "claim_id": claim_id,
                "contract_id": contract_id,
                "epoch": grant.payload["epoch"],
                "package_id": grant.payload["package_id"],
                "worker_id": grant.payload["worker_id"],
            },
            causal_parents=(grant.id, terminal.id),
        )
        foreign_store.append_runtime_record(release)
        runs = task_runs(foreign_store, contract_id)
        row = next(
            item for item in runs["data"]["runs"] if item["claim_id"] == claim_id
        )
        assert runs["ok"] is False
        assert row["status"] == "integrity_error"
        assert release.id in row["fact_ids"]
        assert any(
            issue["code"] == "claim_transition_parent_invalid"
            for issue in row["integrity"]["issues"]
        )
        diagnostics = task_diagnostics(foreign_store, contract_id, claim_id)
        assert diagnostics["ok"] is False
        assert release.id in {
            event["record_id"] for event in diagnostics["data"]["events"]
        }
        assert terminal.id not in {
            event["record_id"] for event in diagnostics["data"]["events"]
        }
    finally:
        foreign_store.close()


@pytest.mark.parametrize(
    ("record_type", "bad_epoch"),
    [
        ("claim.renewed", True),
        ("claim.renewed", 1.0),
        ("claim.released", True),
        ("claim.released", 1.0),
    ],
)
def test_claim_transition_numeric_types_must_match_exactly(
    tmp_path, monkeypatch, record_type, bad_epoch
):
    database, contract_id, claim_id = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    try:
        grant = next(
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("claim_id") == claim_id
        )
        payload = {
            "claim_id": claim_id,
            "contract_id": contract_id,
            "epoch": bad_epoch,
            "package_id": grant.payload["package_id"],
            "worker_id": grant.payload["worker_id"],
        }
        parents = (grant.id,)
        if record_type == "claim.renewed":
            payload["lease_until"] = "2099-01-01T00:00:00Z"
        malformed = create_runtime_record(record_type, payload, causal_parents=parents)
        store.append_runtime_record(malformed)
        runs = task_runs(store, contract_id)
        row = next(
            item for item in runs["data"]["runs"] if item["claim_id"] == claim_id
        )
        assert runs["ok"] is False
        assert row["status"] == "integrity_error"
        assert malformed.id in row["fact_ids"]
        assert any(
            issue["code"] == "claim_transition_payload_mismatch"
            for issue in row["integrity"]["issues"]
        )
    finally:
        store.close()
