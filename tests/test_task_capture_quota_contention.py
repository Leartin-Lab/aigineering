from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest
from click.testing import CliRunner

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock contract
    fcntl = None

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain
from test_fleet_service import _enable_fixture_factory_import


pytestmark = pytest.mark.skipif(fcntl is None, reason="requires POSIX flock")


def test_capture_quota_contention_does_not_gate_configured_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_fixture_factory_import(monkeypatch)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / ".aig" / "identity" / "root.ed25519")
    )

    (tmp_path / "source.txt").write_text("fixture input", encoding="utf-8")
    workers_config = tmp_path / "workers.toml"
    workers_config.write_text(
        '[fleet]\ndb_path=".aig/store.db"\npoll_interval=0.01\n'
        '[[workers]]\nid="capture-fixture-worker"\nkind="local"\n'
        'worker_factory="fixtures.fleet_service_worker:build_worker"\n',
        encoding="utf-8",
    )
    run_config = tmp_path / "run.toml"
    run_config.write_text(
        '[run]\nfleet="workers.toml"\n'
        '[[inputs]]\nname="source"\npath="source.txt"\n'
        '[task]\nname="capture-quota-task"\n'
        'description="Return a result from the fixture input."\n'
        'inputs=["source"]\nactivation="source"\n'
        'outputs=["result"]\nbudget=1\n',
        encoding="utf-8",
    )
    with SQLiteStore(".aig/store.db") as store:
        ensure_local_domain(store)

    log_root = tmp_path / ".aig" / "worker-logs"
    log_root.mkdir(parents=True, mode=0o700)
    lock_path = log_root / ".quota.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            CliRunner().invoke,
            cli,
            [
                "run",
                "--config",
                str(run_config),
                "--wait-timeout",
                "10",
                "--json",
            ],
        )
        result = future.result(timeout=10)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["completed"] is True
        assert payload["worker_errors"] == []
        assert "capture_unavailable" in payload["capture_warnings"]

        output_path = tmp_path / "result.txt"
        result_cli = CliRunner().invoke(
            cli,
            [
                "task",
                "result",
                payload["contract_id"],
                "--output",
                "result",
                "--to",
                str(output_path),
                "--json",
            ],
        )
        assert result_cli.exit_code == 0, result_cli.output
        assert output_path.read_text(encoding="utf-8") == (
            f"service:{payload['contract_id']}:result"
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        pool.shutdown(wait=True, cancel_futures=True)
