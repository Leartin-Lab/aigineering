"""Failure semantics and resource cleanup for operational worker commands."""

import json
from types import SimpleNamespace

from click.testing import CliRunner

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.cli.main import cli
from aigineering.core.candidate_decision import CommitmentDecision
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.protocol.runtime_record import create_runtime_record


class _Store:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def get_contract(self, _contract_id):
        return None

    def get_all_contracts(self):
        return []

    def get_worker_registration(self, _worker_id):
        return None


def test_worker_package_missing_contract_is_nonzero_and_closes_snapshot(monkeypatch):
    store = _Store()
    monkeypatch.setattr("aigineering.cli.worker._persistent_snapshot", lambda: store)

    result = CliRunner().invoke(
        cli, ["worker", "package", "--contract", "missing", "--json"]
    )

    assert result.exit_code == 1
    assert "not found" in json.loads(result.output)["error"]
    assert store.closed


def test_worker_next_relays_claim_decision_before_output_and_closes(monkeypatch):
    store = _Store()
    events = []
    host = SimpleNamespace(
        registration_version="1",
        claim_candidate=lambda **_kwargs: SimpleNamespace(id="signed-claim"),
    )
    monkeypatch.setattr("aigineering.cli.worker._persistent_store", lambda: store)
    monkeypatch.setattr(
        "aigineering.cli.worker.ensure_local_worker_host",
        lambda *_args, **_kwargs: host,
    )
    monkeypatch.setattr(
        "aigineering.cli.worker.claim_next_application_package",
        lambda *args, **kwargs: events.append("claim") or None,
    )
    monkeypatch.setattr(
        "aigineering.cli.worker.sync_runtime", lambda _store: events.append("sync")
    )

    result = CliRunner().invoke(cli, ["worker", "next", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) is None
    assert events == ["claim", "sync"]
    assert store.closed


def test_worker_next_reports_writer_auth_failure_without_traceback(monkeypatch):
    store = _Store()
    host = SimpleNamespace(
        registration_version="1",
        claim_candidate=lambda **_kwargs: SimpleNamespace(id="signed-claim"),
    )
    monkeypatch.setattr("aigineering.cli.worker._persistent_store", lambda: store)
    monkeypatch.setattr(
        "aigineering.cli.worker.ensure_local_worker_host",
        lambda *_args, **_kwargs: host,
    )
    monkeypatch.setattr(
        "aigineering.cli.worker.claim_next_application_package",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("worker registration changed after package selection")
        ),
    )

    result = CliRunner().invoke(cli, ["worker", "next", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "error": "worker registration changed after package selection",
        "status": "error",
    }
    assert "Traceback" not in result.output
    assert store.closed


def test_worker_submit_sync_pending_after_durable_rejection_is_not_reworded(
    monkeypatch,
):
    store = _Store()
    monkeypatch.setattr(store, "get_runtime_revision", lambda: 7, raising=False)
    receipt = create_runtime_record(
        "candidate.received", {"candidate_id": "signed-candidate"}
    )
    rejection = create_runtime_record(
        "candidate.rejected",
        {"candidate_id": "signed-candidate", "reason": "durable rejection"},
        causal_parents=(receipt.id,),
    )
    decision = CommitmentDecision(
        candidate_id="signed-candidate",
        accepted=False,
        runtime_records=(receipt, rejection),
        trace_entries=(),
    )

    def reject_submission(*_args, **_kwargs):
        raise CandidateCommitRejected("durable rejection", decision)

    def unavailable_relay(_store):
        raise QueryProjectionUnavailable("relay interrupted")

    monkeypatch.setattr("aigineering.cli.worker._persistent_store", lambda: store)
    monkeypatch.setattr(
        "aigineering.cli.worker.candidate_proposal_from_dict",
        lambda _data: SimpleNamespace(idempotency_key="signed-key"),
    )
    monkeypatch.setattr(
        "aigineering.cli.worker.submit_worker_proposal_with_decision",
        reject_submission,
    )
    monkeypatch.setattr(
        "aigineering.runtime_services.collaboration_service", unavailable_relay
    )

    result = CliRunner().invoke(cli, ["worker", "submit", "--json", "{}"])

    assert result.exit_code != 0
    pending = json.loads(result.output)
    assert pending["error"] == "redis_sync_pending"
    assert "Redis synchronization pending" in pending["message"]
    assert pending["facts_retained"] is True
    assert pending["candidate_accepted"] is False
    assert pending["candidate_id"] == decision.candidate_id
    assert pending["receipt_id"] == receipt.id
    assert pending["rejection_id"] == rejection.id
    assert pending["revision"] == 7
    assert "status" not in pending
    assert store.closed


def test_worker_submit_invalid_candidate_does_not_open_store(monkeypatch):
    def fail_if_opened():
        raise AssertionError("invalid Candidate must be rejected before Store open")

    monkeypatch.setattr("aigineering.cli.worker._persistent_store", fail_if_opened)

    result = CliRunner().invoke(cli, ["worker", "submit", "--json", "[]"])

    assert result.exit_code == 1
    assert "Invalid CandidateProposal" in result.output
