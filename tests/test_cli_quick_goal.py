"""Quick-goal CLI input, output, and failure evidence behavior."""

import json
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.cli.run import _quick_output_exportable
from aigineering.application import build_worker
from aigineering.core.session import SessionStore
from aigineering.protocol.types import Asset
from aigineering.core.trace import JsonLTraceStore
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Candidate


class _OutputWorker:
    worker_id = "quick-goal-test-worker"

    def __init__(self, raw_output):
        self.raw_output = raw_output
        self.seen = None
        self.history = []

    def invoke(self, contract, disclosed_assets):
        self.seen = (contract, tuple(disclosed_assets))
        self.history.append(self.seen)
        raw = self.raw_output
        if callable(raw):
            raw = raw(contract)
        return Candidate(worker_id=self.worker_id, raw_output=raw)


def test_run_quick_goal_discloses_file_and_exports_markdown(monkeypatch, tmp_path):
    runner = CliRunner()
    worker = _OutputWorker(
        lambda contract: (
            '/exec {"outputs":{"'
            + contract.outputs[0]
            + '":"# Findings\\nEvidence is in the supplied file."}}'
        )
    )
    worker_build_options = {}

    def build_worker(*_args, **kwargs):
        worker_build_options.update(kwargs)
        return worker

    monkeypatch.setattr("aigineering.cli._common.build_worker", build_worker)
    with runner.isolated_filesystem(temp_dir=tmp_path):
        source = tmp_path / "source.md"
        source.write_text("Primary source: observed result 42.", encoding="utf-8")
        output = tmp_path / "result.md"
        result = runner.invoke(
            cli,
            [
                "run",
                "Summarize the evidence",
                "--worker",
                "llm",
                "--model",
                "fixture",
                "--input",
                f"research_notes={source}",
                "--max-output-tokens",
                "8192",
                "--thinking-mode",
                "disabled",
                "--response-format",
                "json_object",
                "--save-config",
                "--output",
                str(output),
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["output_path"] == str(output)
        assert payload["outputs"][0]["content"] == (
            "# Findings\nEvidence is in the supplied file."
        )
        assert output.read_text(encoding="utf-8").startswith("# Findings\n")
        contract, disclosed = worker.seen
        assert len(disclosed) == 2
        file_asset = next(
            asset for asset in disclosed if "observed result 42" in asset.content
        )
        assert file_asset.name == contract.inputs[1]
        assert any(asset.content == "Summarize the evidence" for asset in disclosed)
        assert "research_notes" in contract.description
        assert "Summarize the evidence" in contract.description
        assert contract.name == "build_report"
        assert all(asset.content != "Sample citation database" for asset in disclosed)
        assert worker_build_options["max_output_tokens"] == 8192
        assert worker_build_options["thinking_mode"] == "disabled"
        assert worker_build_options["response_format"] == "json_object"
        store = SQLiteStore(".aig/store.db")
        try:
            config = store.get_assets_by_name("_provider_config_openai")[0]
            config_payload = json.loads(config.content)
            assert config_payload["response_format"] == "json_object"
        finally:
            store.close()


def test_run_without_files_uses_goal_asset_and_unique_slots(monkeypatch):
    runner = CliRunner()
    worker = _OutputWorker(
        lambda contract: '/exec {"outputs":{"' + contract.outputs[0] + '":"ok"}}'
    )
    monkeypatch.setattr(
        "aigineering.cli._common.build_worker", lambda *_args, **_kwargs: worker
    )
    with runner.isolated_filesystem():
        results = [
            runner.invoke(
                cli,
                [
                    "run",
                    "Specific goal",
                    "--worker",
                    "llm",
                    "--model",
                    "fixture",
                    "--json",
                ],
            )
            for _ in range(2)
        ]
        assert all(result.exit_code == 0 for result in results)
        contracts = [item[0] for item in worker.history]
        assert contracts[0].inputs[0].startswith("aig_input_")
        assert "Specific goal" in contracts[0].description
        assert contracts[0].inputs[0] != contracts[1].inputs[0]


def test_invalid_input_has_no_store_or_session_side_effect(monkeypatch, tmp_path):
    opened = False

    def fail_if_opened():
        nonlocal opened
        opened = True
        raise AssertionError("Store must not open for invalid input")

    monkeypatch.setattr("aigineering.cli.run._persistent_store", fail_if_opened)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        bad = tmp_path / "bad.txt"
        bad.write_bytes(b"\xff")
        result = runner.invoke(
            cli,
            ["run", "goal", "--input", f"source={bad}", "--worker", "mock"],
        )
        assert result.exit_code != 0
        assert "valid UTF-8" in result.output
        assert not opened
        assert not SessionStore().list_sessions()


def test_repeated_source_name_uses_fresh_disclosure_slots(monkeypatch, tmp_path):
    runner = CliRunner()
    worker = _OutputWorker(
        lambda contract: '/exec {"outputs":{"' + contract.outputs[0] + '":"ok"}}'
    )
    monkeypatch.setattr(
        "aigineering.cli._common.build_worker", lambda *_args, **_kwargs: worker
    )
    with runner.isolated_filesystem(temp_dir=tmp_path):
        old_file = tmp_path / "old.md"
        new_file = tmp_path / "new.md"
        old_file.write_text("old context", encoding="utf-8")
        new_file.write_text("new context", encoding="utf-8")
        first = runner.invoke(
            cli,
            [
                "run",
                "goal",
                "--input",
                f"brief={old_file}",
                "--worker",
                "llm",
                "--model",
                "fixture",
                "--json",
            ],
        )
        second = runner.invoke(
            cli,
            [
                "run",
                "goal",
                "--input",
                f"brief={new_file}",
                "--worker",
                "llm",
                "--model",
                "fixture",
                "--json",
            ],
        )
        assert first.exit_code == second.exit_code == 0
        disclosed_contents = [asset.content for asset in worker.history[1][1]]
        assert "new context" in disclosed_contents
        assert "old context" not in disclosed_contents


def test_bad_or_duplicate_input_is_rejected_before_store(monkeypatch, tmp_path):
    opened = False

    def fail_if_opened():
        nonlocal opened
        opened = True
        raise AssertionError("Store must not open for invalid input")

    monkeypatch.setattr("aigineering.cli.run._persistent_store", fail_if_opened)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        valid = tmp_path / "valid.txt"
        valid.write_text("content", encoding="utf-8")
        empty = tmp_path / "empty.txt"
        empty.write_text("   ", encoding="utf-8")
        large = tmp_path / "large.txt"
        large.write_bytes(b"x" * (1_048_576 + 1))
        cases = [
            ["--input", f"bad/name={valid}"],
            ["--input", f"same={valid}", "--input", f"same={valid}"],
            ["--input", f"empty={empty}"],
            ["--input", f"large={large}"],
        ]
        for extra in cases:
            result = runner.invoke(cli, ["run", "goal", *extra, "--worker", "mock"])
            assert result.exit_code != 0
        assert not opened
        assert not SessionStore().list_sessions()


def test_failed_quick_goal_keeps_session_and_trace_diagnostics(monkeypatch):
    runner = CliRunner()
    worker = _OutputWorker("not a valid declared output")
    monkeypatch.setattr(
        "aigineering.cli._common.build_worker", lambda *_args, **_kwargs: worker
    )
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["run", "a goal", "--worker", "llm", "--model", "fixture", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["status"] != "complete"
        assert payload["blockers"]
        assert payload["session_id"]
        assert payload["trace_ids"]
        sessions = SessionStore().list_sessions()
        assert sessions[0].id == payload["session_id"]
        trace_path = Path(".aig/traces") / f"{payload['session_id']}.jsonl"
        assert trace_path.exists()
        entries = JsonLTraceStore(str(trace_path)).get_all()
        assert {entry.id for entry in entries} == set(payload["trace_ids"])
        assert payload["rejections"]
        assert "aig task audit" in payload["next_step"]


def test_output_write_failure_preserves_completed_runtime_status(monkeypatch, tmp_path):
    runner = CliRunner()
    worker = _OutputWorker(
        lambda contract: '/exec {"outputs":{"' + contract.outputs[0] + '":"# Done"}}'
    )
    monkeypatch.setattr(
        "aigineering.cli._common.build_worker", lambda *_args, **_kwargs: worker
    )
    output = tmp_path / "denied.md"
    original_open = Path.open

    def failing_open(path, *args, **kwargs):
        if path == output:
            raise OSError("simulated export failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli,
            [
                "run",
                "goal",
                "--worker",
                "llm",
                "--model",
                "fixture",
                "--output",
                str(output),
                "--json",
            ],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["status"] == "output_error"
        assert payload["error"].startswith("could not save Markdown output")
        assert payload["outputs"][0]["asset_id"]
        assert "task audit" not in payload["next_step"]
        assert "aig asset show <output_slot> --json" in payload["next_step"]


def test_run_modes_and_goal_options_are_validated_before_store(monkeypatch):
    def fail_if_opened():
        raise AssertionError("Store must not open for invalid run mode")

    monkeypatch.setattr("aigineering.cli.run._persistent_store", fail_if_opened)
    runner = CliRunner()
    with runner.isolated_filesystem():
        both = runner.invoke(cli, ["run", "goal", "--once", "--worker", "mock"])
        assert both.exit_code != 0
        assert "Choose exactly one" in both.output
        wrong_scope = runner.invoke(
            cli, ["run", "--once", "--input", "source=file.md", "--worker", "mock"]
        )
        assert wrong_scope.exit_code != 0
        assert "only be used with GOAL" in wrong_scope.output
        bad_timeout = runner.invoke(
            cli,
            ["run", "goal", "--wait-timeout", "nan", "--worker", "mock"],
        )
        assert bad_timeout.exit_code != 0
        assert "finite positive" in bad_timeout.output
        invalid_numbers = [
            ["--timeout", "inf"],
            ["--timeout", "0"],
            ["--timeout", "-1"],
            ["--interval", "0"],
            ["--wait-timeout", "-1"],
            ["--max-retries", "-1"],
        ]
        for options in invalid_numbers:
            invalid = runner.invoke(cli, ["run", "goal", *options, "--worker", "mock"])
            assert invalid.exit_code != 0


def test_quick_output_does_not_export_sealed_or_non_promptable_assets():
    base = Asset(id="asset", name="answer", content="private")
    assert _quick_output_exportable(base)
    assert not _quick_output_exportable(
        Asset(id="sealed", name="answer", content="private", disclosure_view="redacted")
    )
    assert not _quick_output_exportable(
        Asset(id="hidden", name="answer", content="private", promptable=False)
    )
    assert not _quick_output_exportable(
        Asset(id="deleted", name="answer", content="private", tombstoned=True)
    )


def test_application_worker_preserves_fractional_timeout_and_response_options():
    worker = build_worker(
        "llm",
        model="fixture-model",
        timeout=0.25,
        max_output_tokens=8192,
        thinking_mode="disabled",
        response_format="json_object",
    )
    assert worker._timeout == 0.25
    assert worker._max_output_tokens == 8192
    assert worker._thinking_mode == "disabled"
    assert worker._response_format == "json_object"
