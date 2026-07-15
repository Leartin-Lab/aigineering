"""Tests for the public CLI demo paths — JSONL persistence."""

import json
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.methods import retry_contract
from aigineering.core.sqlite_store import SQLiteStore
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
        result = runner.invoke(cli, ["run", "test", "--worker", "mock"])

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
        result = runner.invoke(cli, ["run", "test", "--worker", "mock"])

        assert result.exit_code == 0

        trace_files = sorted(Path(".aig/traces").glob("session_*.jsonl"))
        session_files = sorted(Path(".aig/sessions").glob("session_*.json"))
        assert len(trace_files) == 1
        assert len(session_files) == 1

        session_id = trace_files[0].stem
        assert session_files[0].stem == session_id

        store_path = Path(".aig/store.db")
        assert store_path.exists(), "SQLite store DB must exist"

        store = SQLiteStore(str(store_path))
        assets = store.get_assets_by_name("final_report")
        assert len(assets) > 0, "final_report asset must exist in SQLite store"
        contracts = [c for c in store.get_all_contracts() if c.name == "build_report"]
        assert len(contracts) > 0, "build_report contract must exist in SQLite store"
        assert contracts[0].description == "Build a report for goal: test"
        record_types = [
            record.record_type for _, record in store.scan_runtime_records()
        ]
        assert "claim.granted" in record_types
        assert "candidate.received" in record_types
        assert "claim.submitted" in record_types
        store.close()


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
        run_result = runner.invoke(cli, ["run", "test", "--worker", "mock"])
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
        run_result = runner.invoke(cli, ["run", "test", "--worker", "mock"])
        assert run_result.exit_code == 0

        store = SQLiteStore(".aig/store.db")
        contracts = store.get_all_contracts()
        build_report = next(c for c in contracts if c.name == "build_report")
        original_id = build_report.id
        expected_id = retry_contract(build_report).id
        store.close()

        result1 = runner.invoke(cli, ["retry", "--contract", original_id])
        assert result1.exit_code == 0
        assert "Retry contract created:" in result1.output
        retry_id_1 = result1.output.strip().split("\n")[0].split(": ")[-1].strip()

        result2 = runner.invoke(cli, ["retry", "--contract", original_id])
        assert result2.exit_code == 0
        retry_id_2 = result2.output.strip().split("\n")[0].split(": ")[-1].strip()

        assert retry_id_1 == retry_id_2
        assert retry_id_1 == expected_id

        store2 = SQLiteStore(".aig/store.db")
        retry_ids_in_store = [
            c.id for c in store2.get_all_contracts() if c.origin == "retry"
        ]
        assert expected_id in retry_ids_in_store
        retry_events = store2.get_by_contract(original_id)
        assert any(
            e.event_type == "retry_created" and e.relation_target == expected_id
            for e in retry_events
        )
        store2.close()


def test_retry_json_output():
    """aig retry --contract <id> --json returns deterministic retry ID in JSON."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        run_result = runner.invoke(cli, ["run", "test", "--worker", "mock"])
        assert run_result.exit_code == 0

        store = SQLiteStore(".aig/store.db")
        contracts = store.get_all_contracts()
        original = next(c for c in contracts if c.name == "build_report")
        original_id = original.id
        expected_id = retry_contract(original).id
        store.close()

        result = runner.invoke(cli, ["retry", "--contract", original_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["original_contract_id"] == original_id
        assert data["retry_contract_id"] == expected_id


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
                parent_activation,
                parent_disclosure,
                parent_projection,
                method_scheduled,
                child_activation,
                child_disclosure,
                child_projection,
            ],
        )

        result = runner.invoke(cli, ["trace", "--tree"])
        assert result.exit_code == 0

        output = result.output
        assert "contract: contract_parent" in output
        assert "contract: contract_child" in output
        lines = output.split("\n")
        parent_line = [line for line in lines if "contract: contract_parent" in line]
        child_line = [line for line in lines if "contract: contract_child" in line]
        assert parent_line
        assert child_line
        parent_indent = len(parent_line[0]) - len(parent_line[0].lstrip())
        child_indent = len(child_line[0]) - len(child_line[0].lstrip())
        assert child_indent > parent_indent


def test_trace_dag_view():
    """aig trace --dag outputs valid Mermaid flowchart with color-coded nodes."""
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
        assert "```mermaid" in output
        assert "flowchart TD" in output
        assert "contract_A" in output
        assert "contract_B" in output
        assert "contract_C" in output
        assert "contract_D" in output
        assert "plan" in output
        assert "expanded" in output
        assert "classDef completed" in output
        assert "classDef suspended" in output
        assert "classDef active" in output
        assert "suspended" in output
        assert "active" in output


def test_trace_tree_and_dag_include_method_continuation_edges():
    """Continuation contracts are first-class edges in trace projections."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        entries = [
            TraceEntry(
                id="evt_parent_tool",
                contract_id="contract_parent",
                event_type="method_continuation_scheduled",
                relation_type="tool",
                relation_target="contract_continue",
                disclosed_assets=["asset_obs"],
                timestamp="2025-01-01T00:00:00",
            ),
            TraceEntry(
                id="evt_continue_act",
                contract_id="contract_continue",
                event_type="activation",
                timestamp="2025-01-01T00:00:01",
            ),
        ]
        _write_trace_entries(
            Path(".aig/traces/session_continuation.jsonl"),
            entries,
        )

        tree = runner.invoke(cli, ["trace", "--tree"])
        assert tree.exit_code == 0
        assert "contract: contract_parent" in tree.output
        assert "contract: contract_continue" in tree.output
        assert "tool continuation" in tree.output

        lines = tree.output.split("\n")
        parent_line = [line for line in lines if "contract: contract_parent" in line]
        child_line = [line for line in lines if "contract: contract_continue" in line]
        assert parent_line
        assert child_line
        parent_indent = len(parent_line[0]) - len(parent_line[0].lstrip())
        child_indent = len(child_line[0]) - len(child_line[0].lstrip())
        assert child_indent > parent_indent

        dag = runner.invoke(cli, ["trace", "--dag"])
        assert dag.exit_code == 0
        assert "contract_parent" in dag.output
        assert "contract_continue" in dag.output
        assert "tool:continuation" in dag.output


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


# ---------------------------------------------------------------------------
# v0.4.6 — enhanced tree and DAG projection tests
# ---------------------------------------------------------------------------


def test_trace_tree_hierarchy():
    """Tree view uses proper tree-drawing characters and nested indent levels.

    Root contracts at indent 0.  Children indented with ``│   `` prefix.
    Branch characters ``├──`` and ``└──`` used for node headers.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        entries = [
            TraceEntry(
                id="evt_root_act",
                contract_id="root_contract",
                event_type="activation",
                timestamp="2025-01-01T00:00:00",
            ),
            TraceEntry(
                id="evt_root_proj",
                parent_id="evt_root_act",
                contract_id="root_contract",
                event_type="projection",
                accepted_fragments=["frag_root"],
                accepted_asset_names=["root_output"],
                worker_id="worker",
                timestamp="2025-01-01T00:00:01",
            ),
            TraceEntry(
                id="evt_method_1",
                parent_id="evt_root_proj",
                contract_id="root_contract",
                event_type="method_scheduled",
                worker_id="worker",
                relation_type="plan",
                relation_target="child_contract",
                timestamp="2025-01-01T00:00:02",
            ),
            TraceEntry(
                id="evt_method_2",
                parent_id="evt_root_proj",
                contract_id="root_contract",
                event_type="method_scheduled",
                worker_id="worker",
                relation_type="tool",
                relation_target="child_tool",
                timestamp="2025-01-01T00:00:03",
            ),
            TraceEntry(
                id="evt_child_act",
                contract_id="child_contract",
                event_type="activation",
                timestamp="2025-01-01T00:00:04",
            ),
            TraceEntry(
                id="evt_child_proj",
                parent_id="evt_child_act",
                contract_id="child_contract",
                event_type="projection",
                accepted_fragments=["frag_child"],
                accepted_asset_names=["child_output"],
                worker_id="worker",
                timestamp="2025-01-01T00:00:05",
            ),
            TraceEntry(
                id="evt_tool_act",
                contract_id="child_tool",
                event_type="activation",
                timestamp="2025-01-01T00:00:06",
            ),
            TraceEntry(
                id="evt_tool_proj",
                parent_id="evt_tool_act",
                contract_id="child_tool",
                event_type="projection",
                accepted_fragments=["frag_tool"],
                accepted_asset_names=["tool_output"],
                worker_id="worker",
                timestamp="2025-01-01T00:00:07",
            ),
        ]
        _write_trace_entries(
            Path(".aig/traces/session_hierarchy.jsonl"),
            entries,
        )

        result = runner.invoke(cli, ["trace", "--tree"])
        assert result.exit_code == 0

        output = result.output
        lines = output.split("\n")

        root_lines = [line for line in lines if "contract: root_contract" in line]
        child_lines = [line for line in lines if "contract: child_contract" in line]
        tool_lines = [line for line in lines if "contract: child_tool" in line]
        assert len(root_lines) == 1, (
            f"root_contract should appear once, got {root_lines}"
        )
        assert len(child_lines) == 1, (
            f"child_contract should appear once, got {child_lines}"
        )
        assert len(tool_lines) == 1, f"child_tool should appear once, got {tool_lines}"

        root_line = root_lines[0]
        child_line = child_lines[0]
        tool_line = tool_lines[0]

        root_indent = len(root_line) - len(root_line.lstrip())
        child_indent = len(child_line) - len(child_line.lstrip())
        tool_indent = len(tool_line) - len(tool_line.lstrip())
        assert child_indent > root_indent, (
            f"child ({child_indent}) must be deeper than root ({root_indent})"
        )
        assert tool_indent >= child_indent, (
            f"tool ({tool_indent}) must be at least as deep as child ({child_indent})"
        )

        assert "├──" in root_line or "└──" in root_line
        assert "├──" in child_line or "└──" in child_line
        assert "├──" in tool_line or "└──" in tool_line

        assert "│   " in output


def test_trace_tree_shows_method_chain():
    """Tree view reveals plan→tool chain with method and tool events visible."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        entries = [
            TraceEntry(
                id="evt_parent_act",
                contract_id="contract_parent",
                event_type="activation",
                timestamp="2025-01-01T00:00:00",
            ),
            TraceEntry(
                id="evt_parent_disc",
                parent_id="evt_parent_act",
                contract_id="contract_parent",
                event_type="disclosure",
                disclosed_assets=["input_data"],
                worker_id="worker",
                timestamp="2025-01-01T00:00:01",
            ),
            TraceEntry(
                id="evt_parent_proj",
                parent_id="evt_parent_disc",
                contract_id="contract_parent",
                event_type="projection",
                accepted_fragments=["frag_out"],
                accepted_asset_names=["parent_output"],
                worker_id="worker",
                timestamp="2025-01-01T00:00:02",
            ),
            TraceEntry(
                id="evt_method_plan",
                parent_id="evt_parent_proj",
                contract_id="contract_parent",
                event_type="method_scheduled",
                worker_id="worker",
                relation_type="plan",
                relation_target="contract_plan",
                timestamp="2025-01-01T00:00:03",
            ),
            TraceEntry(
                id="evt_plan_act",
                contract_id="contract_plan",
                event_type="activation",
                timestamp="2025-01-01T00:00:04",
            ),
            TraceEntry(
                id="evt_plan_proj",
                parent_id="evt_plan_act",
                contract_id="contract_plan",
                event_type="projection",
                accepted_fragments=["plan_frag"],
                accepted_asset_names=["plan_output"],
                worker_id="worker",
                timestamp="2025-01-01T00:00:05",
            ),
            TraceEntry(
                id="evt_method_tool",
                parent_id="evt_plan_proj",
                contract_id="contract_plan",
                event_type="method_scheduled",
                worker_id="worker",
                relation_type="tool",
                relation_target="contract_tool",
                timestamp="2025-01-01T00:00:06",
            ),
            TraceEntry(
                id="evt_tool_act",
                contract_id="contract_tool",
                event_type="activation",
                timestamp="2025-01-01T00:00:07",
            ),
            TraceEntry(
                id="evt_tool_exec",
                parent_id="evt_tool_act",
                contract_id="contract_tool",
                event_type="tool_executed",
                relation_target="search",
                authority_result="ok",
                accepted_asset_names=["search_result"],
                timestamp="2025-01-01T00:00:08",
            ),
            TraceEntry(
                id="evt_tool_complete",
                parent_id="evt_tool_exec",
                contract_id="contract_tool",
                event_type="complete",
                timestamp="2025-01-01T00:00:09",
            ),
            TraceEntry(
                id="evt_method_resume",
                parent_id="evt_tool_complete",
                contract_id="contract_plan",
                event_type="method_resumed",
                relation_type="tool",
                timestamp="2025-01-01T00:00:10",
            ),
        ]
        _write_trace_entries(
            Path(".aig/traces/session_chain.jsonl"),
            entries,
        )

        result = runner.invoke(cli, ["trace", "--tree"])
        assert result.exit_code == 0

        output = result.output
        assert "contract: contract_parent" in output
        assert "contract: contract_plan" in output
        assert "contract: contract_tool" in output

        assert "/plan" in output
        assert "/tool" in output
        assert "tool_executed" in output
        assert "search" in output
        assert "method_resumed" in output
        assert "complete" in output

        lines = output.split("\n")
        parent_line = [line for line in lines if "contract: contract_parent" in line]
        plan_line = [line for line in lines if "contract: contract_plan" in line]
        tool_line = [line for line in lines if "contract: contract_tool" in line]
        assert len(parent_line) == 1
        assert len(plan_line) == 1
        assert len(tool_line) == 1

        parent_indent = len(parent_line[0]) - len(parent_line[0].lstrip())
        plan_indent = len(plan_line[0]) - len(plan_line[0].lstrip())
        tool_indent = len(tool_line[0]) - len(tool_line[0].lstrip())
        assert plan_indent > parent_indent
        assert tool_indent > plan_indent

        assert "(accepted:" in output
        assert "rejected:" in output


def test_views_regenerated_not_cached():
    """Same trace entries always produce the same DAG output — views are pure projections.

    Running --dag twice over the same file must yield identical output.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        entries = [
            TraceEntry(
                id="evt_1",
                contract_id="contract_X",
                event_type="method_scheduled",
                relation_type="plan",
                relation_target="contract_Y",
                timestamp="2025-01-01T00:00:00",
            ),
            TraceEntry(
                id="evt_2",
                contract_id="contract_Y",
                event_type="activation",
                timestamp="2025-01-01T00:00:01",
            ),
            TraceEntry(
                id="evt_3",
                parent_id="evt_2",
                contract_id="contract_Y",
                event_type="complete",
                timestamp="2025-01-01T00:00:02",
            ),
        ]
        _write_trace_entries(
            Path(".aig/traces/session_cached.jsonl"),
            entries,
        )

        result1 = runner.invoke(cli, ["trace", "--dag"])
        result2 = runner.invoke(cli, ["trace", "--dag"])
        assert result1.exit_code == 0
        assert result2.exit_code == 0
        assert result1.output == result2.output, (
            "DAG view must be deterministic — same trace → same output"
        )

        result_tree1 = runner.invoke(cli, ["trace", "--tree"])
        result_tree2 = runner.invoke(cli, ["trace", "--tree"])
        assert result_tree1.exit_code == 0
        assert result_tree2.exit_code == 0
        assert result_tree1.output == result_tree2.output, (
            "Tree view must be deterministic — same trace → same output"
        )


def test_dag_output_valid_format():
    """DAG output is valid Mermaid flowchart syntax.

    Checks structural elements: fenced code block, flowchart directive,
    node definitions, edge syntax, class definitions, class applications.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        entries = [
            TraceEntry(
                id="evt_a",
                contract_id="alpha",
                event_type="method_scheduled",
                relation_type="plan",
                relation_target="beta",
                timestamp="2025-01-01T00:00:00",
            ),
            TraceEntry(
                id="evt_b",
                contract_id="alpha",
                event_type="method_resumed",
                relation_type="plan",
                timestamp="2025-01-01T00:00:01",
            ),
            TraceEntry(
                id="evt_c",
                contract_id="beta",
                event_type="activation",
                timestamp="2025-01-01T00:00:02",
            ),
            TraceEntry(
                id="evt_d",
                parent_id="evt_c",
                contract_id="beta",
                event_type="complete",
                timestamp="2025-01-01T00:00:03",
            ),
        ]
        _write_trace_entries(
            Path(".aig/traces/session_valid_dag.jsonl"),
            entries,
        )

        result = runner.invoke(cli, ["trace", "--dag"])
        assert result.exit_code == 0

        output = result.output

        assert "```mermaid" in output
        assert "flowchart TD" in output

        import re

        node_defs = re.findall(r'^\s{4}(\w+)\["', output, re.MULTILINE)
        assert "alpha" in node_defs
        assert "beta" in node_defs

        edge_pattern = re.findall(r"^\s{4}(\w+) -->\|", output, re.MULTILINE)
        assert len(edge_pattern) >= 1
        assert "alpha" in edge_pattern

        assert "classDef completed" in output
        assert "classDef suspended" in output
        assert "classDef active" in output

        assert "class alpha " in output
        assert "class beta " in output

        assert "completed" in output or "suspended" in output or "active" in output

        assert "fill:#90EE90" in output
        assert "fill:#FFD700" in output
        assert "fill:#87CEEB" in output


# ---------------------------------------------------------------------------
# v0.5.0-alpha.2 — version metadata
# ---------------------------------------------------------------------------


class TestVersionMetadata:
    """Package version and release metadata."""

    def test_package_version_is_defined(self):
        """__version__ is a valid version string."""
        from aigineering import __version__

        assert __version__ is not None
        assert isinstance(__version__, str)
        assert len(__version__) > 0
        # Must be parseable as at least MAJOR.MINOR.PATCH
        from packaging.version import Version

        parsed = Version(__version__)
        assert parsed.major >= 0
        assert parsed.minor >= 0
        assert parsed.micro >= 0

    def test_package_version_matches_pyproject(self):
        """pyproject.toml version matches __version__ (when installed in dev)."""
        from aigineering import __version__
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        if pyproject.exists():
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            expected = data["project"]["version"]
            # In dev mode (pip install -e .), __version__ reads from metadata
            # which should match pyproject.toml
            assert __version__ == expected or __version__ == "0.0.0-dev", (
                f"__version__={__version__}, pyproject.toml version={expected}"
            )


class TestTrustTierLegacyAliases:
    """Verify TrustTier.from_str() accepts pre-unification tier names."""

    def test_from_str_accepts_legacy_aliases(self):
        """All legacy tier names resolve to canonical TrustTier members."""
        from aigineering.protocol.types import TrustTier

        # Legacy → Canonical mappings
        assert TrustTier.from_str("low") == TrustTier.UNTRUSTED
        assert TrustTier.from_str("medium") == TrustTier.CONFIGURED
        assert TrustTier.from_str("high") == TrustTier.VERIFIED
        assert TrustTier.from_str("worker") == TrustTier.UNTRUSTED
        assert TrustTier.from_str("tool") == TrustTier.CONFIGURED
        assert TrustTier.from_str("trusted") == TrustTier.VERIFIED

    def test_from_str_rejects_unknown_tiers(self):
        """Unknown tier names raise ValueError."""
        from aigineering.protocol.types import TrustTier
        import pytest

        with pytest.raises(ValueError):
            TrustTier.from_str("banana_tier")

    def test_from_str_accepts_canonical_names(self):
        """Canonical tier names resolve correctly."""
        from aigineering.protocol.types import TrustTier

        assert TrustTier.from_str("untrusted") == TrustTier.UNTRUSTED
        assert TrustTier.from_str("observed") == TrustTier.OBSERVED
        assert TrustTier.from_str("configured") == TrustTier.CONFIGURED
        assert TrustTier.from_str("verified") == TrustTier.VERIFIED
        assert TrustTier.from_str("system") == TrustTier.SYSTEM
        assert TrustTier.from_str("human") == TrustTier.HUMAN
