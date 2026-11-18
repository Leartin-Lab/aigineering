"""Review work reserves real causal allowance as an ordinary child Task."""

from __future__ import annotations

import json

from click.testing import CliRunner

from aigineering.core.causal_allowance import allowance_available
from aigineering.core.sqlite_store import SQLiteStore
from test_cli_task_review import _app, _setup


def test_review_request_reserves_once_and_replay_does_not_spend_again(
    tmp_path, monkeypatch
):
    store, database, _genesis, _signer, _operator, _report_actor, source, target = (
        _setup(tmp_path, monkeypatch)
    )
    runner = CliRunner()
    args = [
        "task",
        "request-review",
        source.id,
        "--output",
        "final",
        "--asset",
        target.id,
        "--reason",
        "Check the exact output.",
        "--idempotency-key",
        "funded-review-request",
        "--json",
    ]

    first = runner.invoke(_app(), args)
    assert first.exit_code == 0, first.output
    request = json.loads(first.output)["data"]
    assert request["relation"]["relation"] == "task-review-request"
    assert request["relation"]["budget"] == 1
    assert request["relation"]["reviewer_contract_id"]

    snapshot = SQLiteStore.snapshot_from_path(database)
    source_after_request = snapshot.get_contract(source.id)
    records = tuple(record for _, record in snapshot.scan_runtime_records())
    assert allowance_available(source_after_request, records) == 4
    request_receipts = [
        record
        for _, record in snapshot.scan_runtime_records(record_type="candidate.received")
        if tuple(record.payload.get("effect_types", ()))
        == ("contract.declare", "asset.propose")
    ]
    assert len(request_receipts) == 1
    snapshot.close()

    replay = runner.invoke(_app(), args)
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["data"] == request
    reopened = SQLiteStore.snapshot_from_path(database)
    assert (
        allowance_available(
            reopened.get_contract(source.id),
            tuple(record for _, record in reopened.scan_runtime_records()),
        )
        == 4
    )
    assert len(reopened.scan_runtime_records(record_type="candidate.received")) == len(
        store.scan_runtime_records(record_type="candidate.received")
    )
    reopened.close()
    store.close()
