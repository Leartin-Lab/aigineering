"""The legacy cache command is an alias for Redis maintenance semantics."""

from __future__ import annotations

import json

from click.testing import CliRunner

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.cli import redis as redis_cli
from aigineering.cli.main import cli


def test_cache_status_is_a_redis_status_alias(monkeypatch):
    monkeypatch.setattr(redis_cli, "require_redis", lambda: object())
    runner = CliRunner()
    with runner.isolated_filesystem():
        redis_result = runner.invoke(cli, ["redis", "status", "--json"])
        cache_result = runner.invoke(cli, ["cache", "status", "--json"])

    assert redis_result.exit_code == cache_result.exit_code == 0
    assert json.loads(redis_result.output) == json.loads(cache_result.output)
    assert json.loads(cache_result.output)["required"] is True


def test_cache_status_reports_required_redis_outage_without_secrets(monkeypatch):
    def unavailable():
        raise QueryProjectionUnavailable("Redis collaboration service unavailable")

    monkeypatch.setattr(redis_cli, "require_redis", unavailable)
    result = CliRunner().invoke(cli, ["cache", "status", "--json"])

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["reason"] == "redis_unavailable"
    assert payload["required"] is True
    assert "redis://" not in result.output


def test_cache_maintenance_commands_are_the_redis_commands():
    runner = CliRunner()
    assert "sync" in runner.invoke(cli, ["cache", "--help"]).output
    assert "rebuild" in runner.invoke(cli, ["redis", "--help"]).output
