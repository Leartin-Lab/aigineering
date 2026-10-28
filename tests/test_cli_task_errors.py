from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli


def _forbid_store_open(monkeypatch):
    def fail_if_opened():
        pytest.fail("invalid task input must be rejected before opening the Store")

    monkeypatch.setattr("aigineering.cli.task._persistent_store", fail_if_opened)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--sensitive-input-policy", "[]"),
        ("--sensitive-input-policy", "null"),
        ("--acceptance-policy", '"string"'),
        ("--acceptance-policy", "false"),
        ("--acceptance-policy", "{"),
    ],
)
def test_task_create_rejects_invalid_policy_json_before_store(
    tmp_path, monkeypatch, option, value
):
    monkeypatch.chdir(tmp_path)
    key_path = tmp_path / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    _forbid_store_open(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["task", "create", "--name", "draft", "--output", "answer", option, value],
    )

    assert result.exit_code == 2, result.output
    assert "JSON" in result.output
    assert not (tmp_path / ".aig").exists()
    assert not key_path.exists()


def test_task_create_rejects_invalid_contract_before_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    key_path = tmp_path / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    _forbid_store_open(monkeypatch)

    result = CliRunner().invoke(
        cli,
        [
            "task",
            "create",
            "--name",
            "invalid-budget",
            "--output",
            "answer",
            "--budget",
            "0",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "budget" in result.output.lower()
    assert not (tmp_path / ".aig").exists()
    assert not key_path.exists()


def test_task_create_rejects_non_utf8_description_before_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    key_path = tmp_path / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    description = tmp_path / "description.txt"
    description.write_bytes(b"\xff")
    _forbid_store_open(monkeypatch)

    result = CliRunner().invoke(
        cli,
        [
            "task",
            "create",
            "--name",
            "bad-description",
            "--output",
            "answer",
            "--description-file",
            str(description),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "UTF-8" in result.output
    assert not (tmp_path / ".aig").exists()
    assert not key_path.exists()


@pytest.mark.parametrize("command", ["status", "wait", "audit"])
def test_task_json_not_found_exits_nonzero(monkeypatch, command):
    class MissingStore:
        closed = False

        def get_contract(self, _contract_id):
            return None

        def close(self):
            self.closed = True

    store = MissingStore()
    monkeypatch.setattr("aigineering.cli.task._persistent_snapshot", lambda: store)

    result = CliRunner().invoke(cli, ["task", command, "missing-task", "--json"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.output) == {
        "ok": False,
        "error": "Task 'missing-task' not found.",
    }
    assert store.closed


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--timeout", "nan"),
        ("--timeout", "inf"),
        ("--timeout", "-inf"),
        ("--timeout", "-1"),
        ("--interval", "nan"),
        ("--interval", "inf"),
        ("--interval", "-inf"),
        ("--interval", "0"),
        ("--interval", "-1"),
    ],
)
def test_task_wait_rejects_invalid_duration_before_store(monkeypatch, option, value):
    def fail_if_opened():
        pytest.fail("invalid wait durations must be rejected before opening Store")

    monkeypatch.setattr("aigineering.cli.task._persistent_snapshot", fail_if_opened)

    result = CliRunner().invoke(
        cli,
        ["task", "wait", "some-task", option, value, "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "finite number" in result.output


def test_task_wait_timeout_exits_nonzero(monkeypatch):
    class PendingStore:
        closed = False

        def get_contract(self, _contract_id):
            return object()

        def close(self):
            self.closed = True

    store = PendingStore()
    monkeypatch.setattr("aigineering.cli.task._persistent_snapshot", lambda: store)
    synced = []

    class Collaboration:
        def sync(self):
            synced.append(store)

    monkeypatch.setattr(
        "aigineering.cli.task.collaboration_service", lambda _snapshot: Collaboration()
    )
    monkeypatch.setattr(
        "aigineering.cli.task.project_task_status",
        lambda *_args, **_kwargs: {
            "terminal": False,
            "status": "blocked",
            "integrity": None,
            "ok": True,
        },
    )

    result = CliRunner().invoke(
        cli, ["task", "wait", "pending", "--timeout", "0", "--json"]
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["timed_out"] is True
    assert payload["status"] == "blocked"
    assert payload["ok"] is False
    assert synced == [store]
    assert store.closed


def test_task_wait_terminal_failure_exits_nonzero_and_preserves_status(monkeypatch):
    class FailedStore:
        closed = False

        def get_contract(self, _contract_id):
            return object()

        def close(self):
            self.closed = True

    store = FailedStore()
    monkeypatch.setattr("aigineering.cli.task._persistent_snapshot", lambda: store)
    synced = []

    class Collaboration:
        def sync(self):
            synced.append(store)

    monkeypatch.setattr(
        "aigineering.cli.task.collaboration_service", lambda _snapshot: Collaboration()
    )
    monkeypatch.setattr(
        "aigineering.cli.task.project_task_status",
        lambda *_args, **kwargs: {
            "terminal": True,
            "status": "failed",
            "integrity": {"consistent": True, "status": "consistent"}
            if kwargs.get("check_integrity")
            else None,
            "ok": False,
        },
    )

    result = CliRunner().invoke(cli, ["task", "wait", "failed", "--json"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["status"] == "failed"
    assert synced == [store]
    assert store.closed


def test_task_create_closes_store_after_signed_publication(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    initialized = runner.invoke(cli, ["domain", "init", "--json"])
    assert initialized.exit_code == 0, initialized.output

    from aigineering.cli import task as task_cli

    store = task_cli._persistent_store()
    close = store.close
    store.closed_by_cli = False

    def close_spy():
        store.closed_by_cli = True
        close()

    monkeypatch.setattr(store, "close", close_spy)
    monkeypatch.setattr(task_cli, "_persistent_store", lambda: store)

    result = runner.invoke(
        cli,
        ["task", "create", "--name", "valid", "--output", "answer", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True
    assert store.closed_by_cli
