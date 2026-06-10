"""Tests for the public CLI demo paths — JSONL persistence."""

import json
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.protocol.types import TraceEntry
from aigineering.protocol.wire import trace_entry_to_dict


def _write_trace_entries(trace_file: Path, entries: list[TraceEntry]) -> None:
    """Serialize a list of TraceEntry objects as JSONL to *trace_file*."""
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    with open(trace_file, "w") as f:
        for entry in entries:
            f.write(json.dumps(trace_entry_to_dict(entry)) + "\n")


# ---------------------------------------------------------------------------
# Existing test — updated to exercise the JSONL trace path
# ---------------------------------------------------------------------------

def test_audit_accepts_asset_name_via_asset_option():
    """aig audit --asset-name resolves from a pre-existing JSONL trace."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Build a minimal trace chain: activation → disclosure → projection
        activation = TraceEntry(
            id="act_1",
            contract_id="contract_1",
            event_type="activation",
            timestamp="2025-01-01T00:00:00",
        )
        disclosure = TraceEntry(
            id="disc_1",
            parent_id="act_1",
            contract_id="contract_1",
            event_type="disclosure",
            disclosed_assets=["input_asset"],
            worker_id="mock_worker",
            timestamp="2025-01-01T00:00:01",
        )
        projection = TraceEntry(
            id="proj_1",
            parent_id="disc_1",
            contract_id="contract_1",
            event_type="projection",
            accepted_fragments=["frag_1"],
            accepted_asset_names=["final_report"],
            worker_id="mock_worker",
            timestamp="2025-01-01T00:00:02",
        )

        _write_trace_entries(
            Path(".aig/traces/session_test.jsonl"),
            [activation, disclosure, projection],
        )

        result = runner.invoke(cli, ["audit", "--asset-name", "final_report"])

        assert result.exit_code == 0
        assert "final_report" in result.output
        assert "projection from candidate" in result.output
        assert "disclosure:" in result.output
        assert "activation:" in result.output


# ---------------------------------------------------------------------------
# New tests — JSONL persistence and CLI commands
# ---------------------------------------------------------------------------

def test_run_creates_trace_file():
    """aig run writes a session_*.jsonl file with valid JSON lines."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["run", "test"])

        assert result.exit_code == 0
        assert "Trace saved to" in result.output

        trace_dir = Path(".aig/traces")
        files = sorted(trace_dir.glob("session_*.jsonl"))
        assert len(files) >= 1, f"Expected at least one session file in {trace_dir}"

        for fp in files:
            with open(fp) as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    data = json.loads(stripped)
                    assert "event_type" in data, f"Missing event_type in {fp}"
                    assert "id" in data, f"Missing id in {fp}"
                    assert "contract_id" in data, f"Missing contract_id in {fp}"


def test_run_persists_assets_contracts_and_session_manifest():
    """aig run persists trace, asset/contract store, and session manifest under one session id."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["run", "test"])

        assert result.exit_code == 0

        trace_files = sorted(Path(".aig/traces").glob("session_*.jsonl"))
        session_files = sorted(Path(".aig/sessions").glob("session_*.json"))
        assert len(trace_files) == 1
        assert len(session_files) == 1

        session_id = trace_files[0].stem
        assert session_files[0].stem == session_id

        assets_path = Path(".aig/store/assets.jsonl")
        contracts_path = Path(".aig/store/contracts.jsonl")
        assert assets_path.exists()
        assert contracts_path.exists()

        asset_rows = [
            json.loads(line)
            for line in assets_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        contract_rows = [
            json.loads(line)
            for line in contracts_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        assert any(row["name"] == "final_report" for row in asset_rows)
        assert any(row["name"] == "build_report" for row in contract_rows)


def test_trace_reads_from_latest_file():
    """aig trace reads and displays entries from a pre-existing session file."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        entry = TraceEntry(
            id="entry_1",
            contract_id="contract_1",
            event_type="activation",
            timestamp="2025-01-01T00:00:00",
        )
        _write_trace_entries(
            Path(".aig/traces/session_test.jsonl"),
            [entry],
        )

        result = runner.invoke(cli, ["trace"])

        assert result.exit_code == 0
        assert "activation" in result.output
        assert "contract enabled" in result.output


def test_trace_displays_method_scheduled_event():
    """aig trace displays method decisions as first-class audit events."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        entry = TraceEntry(
            id="method_1",
            contract_id="contract_1",
            event_type="method_scheduled",
            worker_id="mock_worker",
            relation_type="plan",
            relation_target="contract_child",
            timestamp="2025-01-01T00:00:00",
        )
        _write_trace_entries(
            Path(".aig/traces/session_test.jsonl"),
            [entry],
        )

        result = runner.invoke(cli, ["trace"])

        assert result.exit_code == 0
        assert "method_scheduled" in result.output
        assert "/plan scheduled contract_child by mock_worker" in result.output


def test_trace_displays_method_runtime_events():
    """aig trace displays tool execution, resume, and expansion events."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        entries = [
            TraceEntry(
                id="tool_1",
                contract_id="contract_tool",
                event_type="tool_executed",
                accepted_asset_names=["_tool_call_x", "_tool_obs_x"],
                relation_target="lookup",
                authority_result="accepted",
            ),
            TraceEntry(
                id="resume_1",
                contract_id="contract_parent",
                event_type="method_resumed",
                disclosed_assets=["asset_obs"],
                relation_type="tool",
            ),
            TraceEntry(
                id="expand_1",
                contract_id="contract_parent",
                event_type="contracts_expanded",
                relation_target="contract_child",
            ),
        ]
        _write_trace_entries(
            Path(".aig/traces/session_test.jsonl"),
            entries,
        )

        result = runner.invoke(cli, ["trace"])

        assert result.exit_code == 0
        assert "lookup accepted" in result.output
        assert "parent resumed after /tool" in result.output
        assert "planner expanded contracts: contract_child" in result.output


def test_audit_resolves_from_jsonl():
    """aig audit resolves asset by name from JSONL and shows lineage."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        activation = TraceEntry(
            id="act_1",
            contract_id="contract_1",
            event_type="activation",
            timestamp="2025-01-01T00:00:00",
        )
        disclosure = TraceEntry(
            id="disc_1",
            parent_id="act_1",
            contract_id="contract_1",
            event_type="disclosure",
            disclosed_assets=["input_asset"],
            worker_id="mock_worker",
            timestamp="2025-01-01T00:00:01",
        )
        projection = TraceEntry(
            id="proj_1",
            parent_id="disc_1",
            contract_id="contract_1",
            event_type="projection",
            accepted_fragments=["asset_abc"],
            accepted_asset_names=["test_output"],
            worker_id="mock_worker",
            timestamp="2025-01-01T00:00:02",
        )

        _write_trace_entries(
            Path(".aig/traces/session_test.jsonl"),
            [activation, disclosure, projection],
        )

        result = runner.invoke(cli, ["audit", "--asset-name", "test_output"])

        assert result.exit_code == 0
        assert "test_output" in result.output
        assert "projection from candidate" in result.output


def test_trace_no_sessions_shows_error():
    """aig trace shows clear error when no sessions exist (no demo fallback)."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["trace"])

        assert result.exit_code == 0
        assert "No sessions found" in result.output


def test_demo_command_exists():
    """aig demo <goal> runs and exits 0 (preserves quickstart)."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["demo", "test"])

        assert result.exit_code == 0
        assert "Demo completed" in result.output


def test_run_llm_worker_requires_model():
    """aig run --worker llm fails before network use when model is missing."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["run", "test", "--worker", "llm"])

        assert result.exit_code != 0
        assert "--model is required" in result.output


def test_demo_llm_worker_requires_model():
    """aig demo --worker llm fails before network use when model is missing."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["demo", "test", "--worker", "llm"])

        assert result.exit_code != 0
        assert "--model is required" in result.output


def test_replay_valid_session():
    """aig run → aig replay <session_id> shows replay output with consistency."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        run_result = runner.invoke(cli, ["run", "test"])
        assert run_result.exit_code == 0

        # Find the session ID
        ls_result = runner.invoke(cli, ["session", "ls"])
        assert ls_result.exit_code == 0
        # session_ls prints "id  created_at", take the first session_id
        lines = ls_result.output.strip().split("\n")
        assert len(lines) >= 1
        # The first line should look like "session_<timestamp>  <iso_date>"
        session_id = lines[0].split()[0]
        assert session_id.startswith("session_")

        # Replay the session
        replay_result = runner.invoke(cli, ["replay", session_id])
        assert replay_result.exit_code == 0
        assert "Session:" in replay_result.output
        assert session_id in replay_result.output
        assert "Trace entries:" in replay_result.output
        assert "Consistency:" in replay_result.output
        assert "Timeline:" in replay_result.output
