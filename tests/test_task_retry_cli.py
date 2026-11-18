from __future__ import annotations

import json

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain
from aigineering.protocol.identity import contract_from_fields
from aigineering.task_publication import publish_task_contract


class _Collaboration:
    def sync(self):
        return None


def test_task_retry_publishes_signed_successor_and_preserves_source(
    tmp_path, monkeypatch
):
    database = tmp_path / "store.db"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(tmp_path / "identity/root.ed25519"))
    store = SQLiteStore(database)
    ensure_local_domain(store)
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_kw: None
    )
    parent = build_control_plane_contract(
        name="retry allowance source",
        description="Funds the ordinary retry successor.",
        inputs=(),
        outputs=(),
        budget=12,
        activation="",
        tool_scope=(),
        labels=(),
    )
    publish_task_contract(store, parent)
    source = contract_from_fields(
        name="retry source",
        description="A task that can be retried through the ordinary owner.",
        inputs=(),
        outputs=(),
        budget=3,
        activation="",
        tool_scope=(),
        labels=(),
        origin="human",
        parent_id=parent.id,
        minting_authority=(),
    )
    publish_task_contract(store, source)
    source_before = store.get_contract(source.id)
    store.close()

    import aigineering.cli.task_board as board

    monkeypatch.setattr(board, "require_redis", lambda: None)
    monkeypatch.setattr(board, "persistent_store", lambda: SQLiteStore(database))
    monkeypatch.setattr(board, "query_projection", lambda live: live)
    monkeypatch.setattr(board, "collaboration_service", lambda _store: _Collaboration())

    result = CliRunner().invoke(cli, ["task", "retry", source.id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    retry_id = payload["data"]["retry_contract_id"]
    assert retry_id != source.id

    with SQLiteStore.snapshot_from_path(database) as reopened:
        assert reopened.get_contract(source.id) == source_before
        assert reopened.get_contract(retry_id) is not None
        declarations = reopened.scan_runtime_records(record_type="contract.declared")
        assert (
            sum(
                record.payload.get("contract", {}).get("id") == retry_id
                for _, record in declarations
            )
            == 1
        )
        receipts = reopened.scan_runtime_records(record_type="candidate.received")
        heads = reopened.scan_runtime_records(record_type="candidate.committed")
        assert any(
            head.payload.get("candidate_id") == receipt.payload.get("candidate_id")
            for _, head in heads
            for _, receipt in receipts
        )
