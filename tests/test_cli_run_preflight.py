"""Run-specific semantic validation precedes required Redis preflight."""

import click
from click.testing import CliRunner

from aigineering.cli.main import cli


def test_run_config_conflict_is_reported_before_redis_preflight(monkeypatch):
    calls = []

    def require_redis():
        calls.append("redis")
        raise click.ClickException("REDIS_PREFLIGHT_SENTINEL")

    monkeypatch.setattr("aigineering.cli.infrastructure.require_redis", require_redis)
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("fleet.toml", "w", encoding="utf-8") as stream:
            stream.write("not parsed by this preflight check\n")

        result = runner.invoke(
            cli, ["run", "--config", "fleet.toml", "--worker-id", "probe"]
        )

    assert result.exit_code == 2
    assert "--config cannot be combined with --worker-id" in result.output
    assert "REDIS_PREFLIGHT_SENTINEL" not in result.output
    assert calls == []


def test_offline_run_restriction_still_precedes_run_semantic_hook(monkeypatch):
    calls = []

    def require_redis():
        calls.append("redis")
        raise click.ClickException("REDIS_PREFLIGHT_SENTINEL")

    monkeypatch.setattr("aigineering.cli.infrastructure.require_redis", require_redis)
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("fleet.toml", "w", encoding="utf-8") as stream:
            stream.write("not parsed by this preflight check\n")

        result = runner.invoke(
            cli,
            [
                "--offline",
                "run",
                "--config",
                "fleet.toml",
                "--worker-id",
                "probe",
            ],
        )

    assert result.exit_code == 2
    assert "--offline permits only" in result.output
    assert "--config cannot be combined" not in result.output
    assert calls == []
