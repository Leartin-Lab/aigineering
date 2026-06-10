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


def test_trace_no_sessions_runs_demo_fallback():
    """aig trace falls back to the in-memory demo when no session files exist."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["trace"])

        assert result.exit_code == 0
        assert "No trace sessions found" in result.output
