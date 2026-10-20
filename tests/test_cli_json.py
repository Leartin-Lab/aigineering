"""Tests for --json flag on CLI commands."""

import json
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.session import SessionStore
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Session, TraceEntry
from aigineering.protocol.wire import trace_entry_to_dict


def _write_trace_entries(trace_file: Path, entries: list[TraceEntry]) -> None:
    """Serialize a list of TraceEntry objects as JSONL to *trace_file*."""
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    with open(trace_file, "w") as f:
        for entry in entries:
            f.write(json.dumps(trace_entry_to_dict(entry)) + "\n")


def test_run_json_output():
    """aig run <goal> --json produces valid JSON with contract_id, trace_ids, status."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["run", "test", "--worker", "mock", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)

        assert "contract_id" in data
        assert "trace_ids" in data
        assert isinstance(data["trace_ids"], list)
        assert len(data["trace_ids"]) > 0
        assert "status" in data
        assert data["status"] == "complete"
        assert "session_id" in data


def test_run_session_contains_only_root_tree_window():
    """A later run does not inherit unrelated contracts or trace history."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        first = runner.invoke(cli, ["run", "first", "--worker", "mock", "--json"])
        second = runner.invoke(cli, ["run", "second", "--worker", "mock", "--json"])
        assert first.exit_code == second.exit_code == 0
        first_data = json.loads(first.output)
        second_data = json.loads(second.output)
        by_id = {session.id: session for session in SessionStore().list_sessions()}
        first_session = by_id[first_data["session_id"]]
        second_session = by_id[second_data["session_id"]]

        assert first_session.scope == second_session.scope == "root_tree"
        assert first_session.execution_start_revision is not None
        assert (
            first_session.execution_end_revision
            >= first_session.execution_start_revision
        )
        assert (
            second_session.execution_start_revision
            >= first_session.execution_end_revision
        )
        assert first_session.contract_ids == (first_data["contract_id"],)
        assert second_session.contract_ids == (second_data["contract_id"],)
        assert set(second_session.produced_asset_ids) <= set(second_session.asset_ids)
        assert first_session.produced_asset_ids
        assert second_session.produced_asset_ids
        assert set(first_session.produced_asset_ids).isdisjoint(
            second_session.produced_asset_ids
        )
        assert set(first_session.contract_ids).isdisjoint(second_session.contract_ids)
        assert not set(first_session.produced_asset_ids) & set(second_session.asset_ids)
        assert set(first_data["trace_ids"]).isdisjoint(second_data["trace_ids"])

        trace_file = f".aig/traces/{second_data['session_id']}.jsonl"
        with open(trace_file, encoding="utf-8") as stream:
            trace_ids = {json.loads(line)["id"] for line in stream if line.strip()}
        assert trace_ids == set(second_session.trace_ids)


def test_trace_json_output():
    """aig trace --json produces a JSON array with rejected_fragments on entries."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(cli, ["run", "test", "--worker", "mock"])
        result = runner.invoke(cli, ["trace", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)

        assert isinstance(data, list)
        assert len(data) > 0
        for entry in data:
            assert isinstance(entry, dict)
            assert "event_type" in entry
            assert "rejected_fragments" in entry
            assert isinstance(entry["rejected_fragments"], list)


def test_sealed_config_not_in_json():
    """aig session ls --json must NOT include config_snapshot or worker_snapshot."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(cli, ["run", "test", "--worker", "mock"])

        result = runner.invoke(cli, ["session", "ls", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) > 0

        for session in data:
            assert "config_snapshot" not in session, (
                "config_snapshot must not be leaked in JSON output"
            )
            assert "worker_snapshot" not in session, (
                "worker_snapshot must not be leaked in JSON output"
            )

        # Also verify that the raw stdout does not contain any API key pattern
        assert "sk-" not in result.output.lower()


def test_session_ls_json():
    """aig session ls --json produces a valid JSON array of session objects."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(cli, ["run", "test", "--worker", "mock"])

        result = runner.invoke(cli, ["session", "ls", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)

        assert isinstance(data, list)
        assert len(data) > 0

        for session in data:
            assert "id" in session
            assert "root_contract_id" in session
            assert "created_at" in session
            assert isinstance(session["contract_ids"], list)
            assert isinstance(session["asset_ids"], list)
            assert isinstance(session["trace_ids"], list)


def test_audit_json_output():
    """aig audit --json produces valid JSON with asset_id, asset_name, lineage."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        run_result = runner.invoke(cli, ["run", "test", "--worker", "mock", "--json"])
        assert run_result.exit_code == 0, run_result.output
        run_payload = json.loads(run_result.output)
        store = SQLiteStore(".aig/store.db")
        root = store.get_contract(run_payload["contract_id"])
        assert root is not None
        output_name = root.outputs[0]
        store.close()

        result = runner.invoke(
            cli,
            ["audit", "--asset-name", output_name, "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)

        assert isinstance(data, dict)
        assert "asset_id" in data
        assert "asset_name" in data
        assert data["asset_name"] == output_name
        assert "lineage" in data
        assert isinstance(data["lineage"], list)
        assert len(data["lineage"]) > 0

        for entry in data["lineage"]:
            assert "event_type" in entry
            assert "id" in entry


def test_replay_json_output():
    """aig replay <session_id> --json produces valid JSON with session, entries, consistent."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(cli, ["run", "test", "--worker", "mock"])

        # Grab the session ID from session ls --json
        ls_result = runner.invoke(cli, ["session", "ls", "--json"])
        sessions = json.loads(ls_result.output)
        assert len(sessions) > 0
        session_id = sessions[0]["id"]

        result = runner.invoke(cli, ["replay", session_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)

        assert isinstance(data, dict)
        assert "session" in data
        assert "entries" in data
        assert "consistent" in data
        assert "accepted_count" in data
        assert "rejected_count" in data

        # Sealed config must not be in the session sub-object
        if data["session"] is not None:
            assert "config_snapshot" not in data["session"]
            assert "worker_snapshot" not in data["session"]

        assert isinstance(data["entries"], list)
        assert len(data["entries"]) > 0


def test_run_json_no_entries_handled():
    """aig run --json handles edge case where a run produces no entries gracefully."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # When no trace entries are recorded, JSON should still be valid
        result = runner.invoke(cli, ["run", "test", "--worker", "mock", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "status" in data


def test_trace_json_no_sessions():
    """aig trace --json returns empty array when no sessions exist."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["trace", "--json"])
        # aig trace exits 0 even when no sessions found
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 0


def test_trace_json_session_manifest_missing_rc1():
    """aig trace --session --json with no session manifest exits 1 (C3)."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["trace", "--session", "missing", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data == {"error": "Session 'missing' not found."}
        assert result.stderr == ""


def test_trace_json_session_export_missing_rc1():
    """aig trace --session --json with a manifest but no export exits 1 (C3)."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Manifest exists; no .aig/traces directory, so neither the direct
        # export nor the fallback scan can produce a store.
        SessionStore().create_session(
            Session(id="missing", trace_ids=("trace_missing",))
        )

        result = runner.invoke(cli, ["trace", "--session", "missing", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data == {"error": "Session 'missing' not found."}
        assert result.stderr == ""


def test_audit_json_error_message():
    """aig audit --json returns error dict with exit 1 when asset not found."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(cli, ["run", "test", "--worker", "mock"])

        result = runner.invoke(
            cli,
            ["audit", "--asset-name", "nonexistent_asset", "--json"],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data


def test_replay_json_error_message():
    """aig replay --json returns error dict when session not found."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["replay", "nonexistent_session", "--json"],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data


def test_replay_all_json():
    """aig replay --all --json produces JSON array."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(cli, ["run", "test", "--worker", "mock"])

        result = runner.invoke(cli, ["replay", "--all", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)

        assert isinstance(data, list)
        assert len(data) > 0

        for item in data:
            assert "session" in item
            assert "entries" in item
            assert "consistent" in item


def _write_audit_trace_fixture() -> None:
    """Write a readable latest-session trace with a named projection asset."""
    activation = TraceEntry(
        id="act_1",
        contract_id="contract_1",
        event_type="activation",
        timestamp="2025-01-01T00:00:00",
    )
    projection = TraceEntry(
        id="proj_1",
        parent_id="act_1",
        contract_id="contract_1",
        event_type="projection",
        accepted_fragments=["asset_abc"],
        accepted_asset_names=["test_output"],
        worker_id="mock_worker",
        timestamp="2025-01-01T00:00:01",
    )
    _write_trace_entries(
        Path(".aig/traces/session_test.jsonl"),
        [activation, projection],
    )


def test_audit_session_missing_json_exits_1():
    """audit --session <missing> --json prints the error object then exits 1."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["audit", "--session", "missing", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {"error": "Session 'missing' not found."}
        assert result.stderr == ""


def test_audit_session_manifest_without_export_json_exits_1():
    """audit --session --json with a manifest but no matching export exits 1.

    The fixture rules out both the direct export hit and the fallback
    trace-id scan, so the miss comes from a fully exhausted search.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        unrelated = TraceEntry(
            id="unrelated_1",
            contract_id="contract_other",
            event_type="activation",
            timestamp="2025-01-01T00:00:00",
        )
        _write_trace_entries(
            Path(".aig/traces/session_other.jsonl"),
            [unrelated],
        )
        SessionStore(sessions_dir=".aig/sessions").create_session(
            Session(id="sess_ghost", trace_ids=("t_ghost",))
        )

        result = runner.invoke(cli, ["audit", "--session", "sess_ghost", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {"error": "Session 'sess_ghost' not found."}
        assert result.stderr == ""


def test_audit_asset_id_not_found_json_exits_1():
    """audit --asset <unknown> --json prints the error object then exits 1."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write_audit_trace_fixture()

        result = runner.invoke(cli, ["audit", "--asset", "nope", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {
            "error": "No asset found with id or name 'nope'"
        }
        assert result.stderr == ""


def test_audit_asset_name_not_found_json_exits_1():
    """audit --asset-name <unknown> --json prints the error object then exits 1."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write_audit_trace_fixture()

        result = runner.invoke(cli, ["audit", "--asset-name", "nope", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {"error": "No asset found with name 'nope'"}
        assert result.stderr == ""


def test_audit_target_unresolved_json_exits_1(monkeypatch):
    """Defensive target seam: unresolved target prints error object then exits 1."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write_audit_trace_fixture()
        monkeypatch.setattr(
            "aigineering.cli.trace._resolve_asset_by_name",
            lambda asset_name, trace_store: ("", "ghost"),
        )

        result = runner.invoke(cli, ["audit", "--asset-name", "test_output", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {"error": "Could not determine target asset."}
        assert result.stderr == ""


def test_audit_empty_lineage_json_seam_exits_0(monkeypatch):
    """Seam regression: resolved asset with empty lineage stays rc=0 (JSON)."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write_audit_trace_fixture()
        monkeypatch.setattr(
            "aigineering.cli.trace._resolve_asset_by_name",
            lambda asset_name, trace_store: ("ghost_asset", "ghost"),
        )

        result = runner.invoke(cli, ["audit", "--asset-name", "test_output", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == {
            "asset_id": "ghost_asset",
            "asset_name": "ghost",
            "lineage": [],
        }
        assert result.stderr == ""
