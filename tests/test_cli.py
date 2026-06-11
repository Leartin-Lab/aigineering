"""Tests for the public CLI demo paths — JSONL persistence."""

import json
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.ids import hash_retry
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


# ---------------------------------------------------------------------------
# v0.3.18 — retry, trace --tree, trace --dag
# ---------------------------------------------------------------------------

def test_retry_creates_deterministic_contract():
    """aig retry --contract <id> creates a contract with deterministic retry ID."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        run_result = runner.invoke(cli, ["run", "test"])
        assert run_result.exit_code == 0

        contracts_path = Path(".aig/store/contracts.jsonl")
        contract_rows = [
            json.loads(line)
            for line in contracts_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        build_report = [r for r in contract_rows if r["name"] == "build_report"][0]
        original_id = build_report["id"]

        result1 = runner.invoke(cli, ["retry", "--contract", original_id])
        assert result1.exit_code == 0
        assert "Retry contract created:" in result1.output
        retry_id_1 = result1.output.strip().split("\n")[0].split(": ")[-1].strip()

        result2 = runner.invoke(cli, ["retry", "--contract", original_id])
        assert result2.exit_code == 0
        retry_id_2 = result2.output.strip().split("\n")[0].split(": ")[-1].strip()

        assert retry_id_1 == retry_id_2
        assert retry_id_1.startswith("retry:")
        assert retry_id_1 == hash_retry(original_id)

        expected_id = hash_retry(original_id)
        assert retry_id_1 == expected_id

        stored = [
            json.loads(line)
            for line in contracts_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        retry_ids_in_store = [r["id"] for r in stored if r["id"].startswith("retry:")]
        assert expected_id in retry_ids_in_store


def test_retry_json_output():
    """aig retry --contract <id> --json returns deterministic retry ID in JSON."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        run_result = runner.invoke(cli, ["run", "test"])
        assert run_result.exit_code == 0

        contracts_path = Path(".aig/store/contracts.jsonl")
        contract_rows = [
            json.loads(line)
            for line in contracts_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        original_id = [r for r in contract_rows if r["name"] == "build_report"][0]["id"]

        result = runner.invoke(cli, ["retry", "--contract", original_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["original_contract_id"] == original_id
        assert data["retry_contract_id"].startswith("retry:")
        assert data["retry_contract_id"] == hash_retry(original_id)


def test_retry_contract_not_found():
    """aig retry --contract <nonexistent> reports error."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["retry", "--contract", "nonexistent"])
        assert result.exit_code == 0
        assert "not found" in result.output


def test_trace_tree_view():
    """aig trace --tree shows hierarchical parent→child→method→tool chain."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        parent_activation = TraceEntry(
            id="evt_parent_act",
            contract_id="contract_parent",
            event_type="activation",
            timestamp="2025-01-01T00:00:00",
        )
        parent_disclosure = TraceEntry(
            id="evt_parent_disc",
            parent_id="evt_parent_act",
            contract_id="contract_parent",
            event_type="disclosure",
            disclosed_assets=["input_asset"],
            worker_id="mock_worker",
            timestamp="2025-01-01T00:00:01",
        )
        parent_projection = TraceEntry(
            id="evt_parent_proj",
            parent_id="evt_parent_disc",
            contract_id="contract_parent",
            event_type="projection",
            accepted_fragments=["frag_parent"],
            accepted_asset_names=["parent_output"],
            worker_id="mock_worker",
            timestamp="2025-01-01T00:00:02",
        )
        method_scheduled = TraceEntry(
            id="evt_method",
            parent_id="evt_parent_proj",
            contract_id="contract_parent",
            event_type="method_scheduled",
            worker_id="mock_worker",
            relation_type="plan",
            relation_target="contract_child",
            timestamp="2025-01-01T00:00:03",
        )
        child_activation = TraceEntry(
            id="evt_child_act",
            contract_id="contract_child",
            event_type="activation",
            timestamp="2025-01-01T00:00:04",
        )
        child_disclosure = TraceEntry(
            id="evt_child_disc",
            parent_id="evt_child_act",
            contract_id="contract_child",
            event_type="disclosure",
            disclosed_assets=["parent_output"],
            worker_id="mock_worker",
            timestamp="2025-01-01T00:00:05",
        )
        child_projection = TraceEntry(
            id="evt_child_proj",
            parent_id="evt_child_disc",
            contract_id="contract_child",
            event_type="projection",
            accepted_fragments=["frag_child"],
            accepted_asset_names=["child_output"],
            worker_id="mock_worker",
            timestamp="2025-01-01T00:00:06",
        )

        _write_trace_entries(
            Path(".aig/traces/session_tree.jsonl"),
            [
                parent_activation, parent_disclosure, parent_projection,
                method_scheduled,
                child_activation, child_disclosure, child_projection,
            ],
        )

        result = runner.invoke(cli, ["trace", "--tree"])
        assert result.exit_code == 0

        output = result.output
        assert "contract: contract_parent" in output
        assert "contract: contract_child" in output
        lines = output.split("\n")
        parent_line = [l for l in lines if "contract: contract_parent" in l]
        child_line = [l for l in lines if "contract: contract_child" in l]
        assert parent_line
        assert child_line
        parent_indent = len(parent_line[0]) - len(parent_line[0].lstrip())
        child_indent = len(child_line[0]) - len(child_line[0].lstrip())
        assert child_indent > parent_indent


def test_trace_dag_view():
    """aig trace --dag shows graph edges connecting parent→child contracts."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        entries = [
            TraceEntry(
                id="evt_1",
                contract_id="contract_A",
                event_type="method_scheduled",
                relation_type="plan",
                relation_target="contract_B",
                timestamp="2025-01-01T00:00:00",
            ),
            TraceEntry(
                id="evt_2",
                contract_id="contract_B",
                event_type="contracts_expanded",
                relation_target="contract_C contract_D",
                timestamp="2025-01-01T00:00:01",
            ),
        ]
        _write_trace_entries(
            Path(".aig/traces/session_dag.jsonl"),
            entries,
        )

        result = runner.invoke(cli, ["trace", "--dag"])
        assert result.exit_code == 0

        output = result.output
        assert "Contract DAG edges:" in output
        assert "contract_A" in output
        assert "contract_B" in output
        assert "contract_C" in output
        assert "contract_D" in output
        assert "plan" in output
        assert "expanded" in output


def test_trace_dag_empty():
    """aig trace --dag with no edges shows informative message."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        entry = TraceEntry(
            id="evt_1",
            contract_id="contract_solo",
            event_type="activation",
            timestamp="2025-01-01T00:00:00",
        )
        _write_trace_entries(
            Path(".aig/traces/session_solo.jsonl"),
            [entry],
        )

        result = runner.invoke(cli, ["trace", "--dag"])
        assert result.exit_code == 0
        assert "no parent→child contract edges found" in result.output


def test_views_are_derived_not_stored_truth():
    """Tree and DAG views are pure projections over trace entries.

    There is no separate DAG storage — changing trace entries changes the
    view output.  Both --tree and --dag are computed on the fly from the
    same JSONL trace file.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        entries_v1 = [
            TraceEntry(
                id="evt_v1",
                contract_id="contract_root",
                event_type="method_scheduled",
                relation_type="plan",
                relation_target="contract_alpha",
                timestamp="2025-01-01T00:00:00",
            ),
        ]
        trace_path = Path(".aig/traces/session_derived.jsonl")
        _write_trace_entries(trace_path, entries_v1)

        result_v1 = runner.invoke(cli, ["trace", "--dag"])
        assert result_v1.exit_code == 0
        assert "contract_alpha" in result_v1.output
        assert "contract_beta" not in result_v1.output

        entries_v2 = [
            TraceEntry(
                id="evt_v2",
                contract_id="contract_root",
                event_type="method_scheduled",
                relation_type="plan",
                relation_target="contract_beta",
                timestamp="2025-01-01T00:00:00",
            ),
        ]
        _write_trace_entries(trace_path, entries_v2)

        result_v2 = runner.invoke(cli, ["trace", "--dag"])
        assert result_v2.exit_code == 0
        assert "contract_beta" in result_v2.output
        assert "contract_alpha" not in result_v2.output

        result_tree_v2 = runner.invoke(cli, ["trace", "--tree"])
        assert result_tree_v2.exit_code == 0
        assert "contract: contract_root" in result_tree_v2.output
        assert "contract: contract_beta" in result_tree_v2.output
