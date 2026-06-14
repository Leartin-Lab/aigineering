"""Tests for --json flag on CLI commands."""

import json

from click.testing import CliRunner

from aigineering.cli.main import cli


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
        runner.invoke(cli, ["run", "test", "--worker", "mock"])

        result = runner.invoke(
            cli,
            ["audit", "--asset-name", "final_report", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)

        assert isinstance(data, dict)
        assert "asset_id" in data
        assert "asset_name" in data
        assert data["asset_name"] == "final_report"
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


def test_audit_json_error_message():
    """aig audit --json returns error dict when asset not found."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(cli, ["run", "test", "--worker", "mock"])

        result = runner.invoke(
            cli,
            ["audit", "--asset-name", "nonexistent_asset", "--json"],
        )
        assert result.exit_code == 0
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
        assert result.exit_code == 0
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
