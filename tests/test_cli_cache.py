"""CLI observability for the disposable query projection."""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli


def test_cache_status_without_configuration_uses_sqlite(monkeypatch):
    monkeypatch.delenv("AIGINEERING_REDIS_URL", raising=False)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["cache", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["backend"] == "sqlite"
    assert payload["configured"] is False
    assert payload["current"] is True


def test_cache_status_before_domain_initialization_is_explicit(monkeypatch):
    monkeypatch.setenv("AIGINEERING_REDIS_URL", "redis://unused.invalid:6379/0")
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["cache", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["backend"] == "sqlite"
    assert payload["configured"] is True
    assert payload["reason"] == "domain_uninitialized"


def test_cache_rebuild_without_configuration_fails_visibly(monkeypatch):
    monkeypatch.delenv("AIGINEERING_REDIS_URL", raising=False)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["cache", "rebuild"])

    assert result.exit_code != 0
    assert "AIGINEERING_REDIS_URL is not configured" in result.output


def test_cache_rebuild_and_status_against_real_redis(monkeypatch):
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL to run Redis integration")
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.flushdb()
    monkeypatch.setenv("AIGINEERING_REDIS_URL", redis_url)
    runner = CliRunner()
    with runner.isolated_filesystem():
        initialized = runner.invoke(cli, ["domain", "init", "--json"])
        assert initialized.exit_code == 0, initialized.output
        rebuilt = runner.invoke(cli, ["cache", "rebuild", "--json"])
        status = runner.invoke(cli, ["cache", "status", "--json"])

    assert rebuilt.exit_code == 0, rebuilt.output
    rebuild_payload = json.loads(rebuilt.output)
    assert rebuild_payload["backend"] == "redis"
    assert rebuild_payload["current"] is True
    assert rebuild_payload["rebuilt_digest"]
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert (
        status_payload["projection_revision"]
        == status_payload["authoritative_revision"]
    )
