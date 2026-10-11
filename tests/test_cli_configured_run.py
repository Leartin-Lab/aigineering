"""CLI entrypoint behavior for configured scenarios and fleet resumption."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.types import Candidate


class _WritingWorker:
    worker_id = "cli-configured-worker"

    def registration(self):
        return WorkerRegistration(self.worker_id)

    def invoke(self, contract, disclosed_assets):
        del disclosed_assets
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps({"outputs": {name: "accepted" for name in contract.outputs}}),
        )


def test_configured_run_delegates_new_preset(monkeypatch, tmp_path):
    config = tmp_path / "scenario.toml"
    config.write_text("[run]\nfleet='workers.toml'\n", encoding="utf-8")
    prepared = object()
    calls: list[tuple] = []
    monkeypatch.setattr(
        "aigineering.cli.run.prepare_configured_run",
        lambda path, *, resume_task_id=None: (
            calls.append(("prepare", path, resume_task_id)) or prepared
        ),
    )
    monkeypatch.setattr(
        "aigineering.cli.run.run_configured",
        lambda value, *, timeout: (
            calls.append(("run", value, timeout))
            or SimpleNamespace(
                contract_id="contract:test",
                status="completed",
                completed=True,
                timed_out=False,
                worker_errors=(),
                worker_failure_details=(
                    {
                        "kind": "invocation_failure",
                        "status": "invocation_failed",
                        "contract_id": "task:failure",
                        "claim_id": "lease:" + "a" * 64,
                        "category": "provider_error",
                    },
                ),
                capture_warnings=("capture_unavailable",),
            )
        ),
    )

    result = CliRunner().invoke(
        cli, ["run", "--config", str(config), "--wait-timeout", "2", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert '"contract_id": "contract:test"' in result.output
    payload = json.loads(result.output)
    assert payload["capture_warnings"] == ["capture_unavailable"]
    assert payload["worker_failure_details"][0]["claim_id"] == "lease:" + "a" * 64
    assert calls == [("prepare", config, None), ("run", prepared, 2.0)]


def test_configured_run_accepts_task_and_resume_alias(monkeypatch, tmp_path):
    config = tmp_path / "workers.toml"
    config.write_text("", encoding="utf-8")
    resumes: list[str | None] = []
    monkeypatch.setattr(
        "aigineering.cli.run.prepare_configured_run",
        lambda _path, *, resume_task_id=None: (
            resumes.append(resume_task_id) or object()
        ),
    )
    monkeypatch.setattr(
        "aigineering.cli.run.run_configured",
        lambda *_args, **_kwargs: SimpleNamespace(
            contract_id="contract:test",
            status="completed",
            completed=True,
            timed_out=False,
            worker_errors=(),
            worker_failure_details=(
                {
                    "kind": "invocation_failure",
                    "status": "invocation_failed",
                    "contract_id": "task:failure",
                    "claim_id": "lease:" + "b" * 64,
                    "category": "provider_error",
                },
            ),
            capture_warnings=(),
        ),
    )

    runner = CliRunner()
    for flag in ("--task", "--resume"):
        result = runner.invoke(cli, ["run", "--config", str(config), flag, "target"])
        assert result.exit_code == 0, result.output
        assert "worker error:" not in result.output
        assert "worker failure detail:" in result.output
        assert "lease:" + "b" * 64 in result.output
    assert resumes == ["target", "target"]


def test_resume_alias_preserves_legacy_single_worker_task_mode(monkeypatch):
    targets: list[str | None] = []
    monkeypatch.setattr(
        "aigineering.cli.run._run_task_pool",
        lambda **kwargs: targets.append(kwargs["target_task_id"]),
    )
    result = CliRunner().invoke(cli, ["run", "--resume", "contract:legacy"])
    assert result.exit_code == 0, result.output
    assert targets == ["contract:legacy"]


def test_empty_task_and_resume_ids_reject_before_configured_service(
    monkeypatch, tmp_path
):
    config = tmp_path / "workers.toml"
    config.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "aigineering.cli.run.prepare_configured_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty task IDs must fail before service setup")
        ),
    )
    runner = CliRunner()
    for args in (("--task", " "), ("--resume", "")):
        result = runner.invoke(cli, ["run", "--config", str(config), *args])
        assert result.exit_code != 0
        assert "must not be empty or whitespace" in result.output


def test_configured_run_rejects_conflicting_modes_before_service(monkeypatch, tmp_path):
    config = tmp_path / "workers.toml"
    config.write_text("", encoding="utf-8")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("invalid CLI combination must fail before service")

    monkeypatch.setattr("aigineering.cli.run.prepare_configured_run", unexpected)
    runner = CliRunner()
    for args in (
        ["goal"],
        ["--once"],
        ["--input", "x=input.txt"],
        ["--output", "out.md"],
        ["--worker", "mock"],
        ["--model", "explicit"],
        ["--tool-registry", "package:factory"],
        ["--behavior-label", "review"],
        ["--save-config"],
        ["--interval", "0.2"],
        ["--task", "one", "--resume", "two"],
    ):
        result = runner.invoke(cli, ["run", "--config", str(config), *args])
        assert result.exit_code != 0
        assert (
            "cannot be combined" in result.output or "Choose only one" in result.output
        )


def test_workspace_option_restores_process_cwd_after_invocation(tmp_path):
    runner = CliRunner()
    original = Path.cwd()
    workspace = tmp_path / "work"
    workspace.mkdir()

    import click

    @click.command("cwd-probe")
    def cwd_probe():
        click.echo(Path.cwd())

    cli.add_command(cwd_probe)
    try:
        result = runner.invoke(cli, ["-C", str(workspace), "cwd-probe"])
    finally:
        cli.commands.pop("cwd-probe", None)

    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(workspace.resolve())
    assert Path.cwd() == original


def test_workspace_relative_config_runs_real_local_worker(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(workspace / "identity" / "root.ed25519")
    )
    monkeypatch.setattr(
        "aigineering.fleet_config.build_fleet_worker",
        lambda _spec: _WritingWorker(),
    )
    (workspace / "workers.toml").write_text(
        '[fleet]\ndb_path=".aig/store.db"\npoll_interval=0.01\n'
        '[[workers]]\nid="cli-configured-worker"\nkind="local"\n'
        'worker_factory="fixture:worker"\n',
        encoding="utf-8",
    )
    (workspace / "input.txt").write_text("local fixture input", encoding="utf-8")
    (workspace / "run.toml").write_text(
        '[run]\nfleet="workers.toml"\n'
        '[[inputs]]\nname="source"\npath="input.txt"\n'
        '[task]\nname="cli-root"\ninputs=["source"]\nactivation="source"\n'
        'outputs=["result"]\nbudget=1\n',
        encoding="utf-8",
    )
    original = Path.cwd()

    result = CliRunner().invoke(
        cli,
        ["-C", str(workspace), "run", "--config", "run.toml", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "complete"
    assert payload["completed"] is True
    assert Path.cwd() == original


def test_workspace_is_restored_when_subcommand_validation_fails(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = Path.cwd()

    result = CliRunner().invoke(
        cli,
        ["-C", str(workspace), "run", "--config", "missing.toml"],
    )

    assert result.exit_code != 0
    assert Path.cwd() == original


def test_configured_timeout_must_be_finite_and_positive(monkeypatch, tmp_path):
    config = tmp_path / "workers.toml"
    config.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "aigineering.cli.run.prepare_configured_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("timeout must validate before setup")
        ),
    )
    for value in ("0", "-1", "inf", "nan"):
        result = CliRunner().invoke(
            cli,
            ["run", "--config", str(config), "--wait-timeout", value],
        )
        assert result.exit_code != 0
        assert "finite positive" in result.output


def test_legacy_fleet_run_delegates_and_reports_incomplete_result(
    monkeypatch, tmp_path
):
    config = tmp_path / "workers.toml"
    config.write_text("", encoding="utf-8")
    prepared = object()
    calls: list[tuple] = []
    monkeypatch.setattr(
        "aigineering.cli.fleet.prepare_configured_run",
        lambda path, *, resume_task_id=None: (
            calls.append(("prepare", path, resume_task_id)) or prepared
        ),
    )
    monkeypatch.setattr(
        "aigineering.cli.fleet.run_configured",
        lambda value, *, timeout: (
            calls.append(("run", value, timeout))
            or SimpleNamespace(
                contract_id="contract:blocked",
                status="blocked",
                completed=False,
                timed_out=True,
                worker_errors=("no eligible worker",),
                worker_failure_details=(),
                capture_warnings=("storage_limit",),
            )
        ),
    )

    result = CliRunner().invoke(
        cli,
        ["fleet", "run", "--config", str(config), "--task", "contract:blocked"],
    )

    assert result.exit_code == 1
    assert "fleet blocked: contract:blocked" in result.output
    assert "worker error: no eligible worker" in result.output
    assert "capture warning: storage_limit" in result.output
    assert calls == [
        ("prepare", str(config), "contract:blocked"),
        ("run", prepared, 300.0),
    ]

    json_result = CliRunner().invoke(
        cli,
        [
            "fleet",
            "run",
            "--config",
            str(config),
            "--task",
            "contract:blocked",
            "--json",
        ],
    )
    assert json_result.exit_code == 1
    assert json.loads(json_result.output)["worker_failure_details"] == []
