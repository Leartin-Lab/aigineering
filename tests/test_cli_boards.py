"""End-to-end CLI tests for named board routing and lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli


@pytest.fixture(autouse=True)
def isolate_actor_key_path(monkeypatch):
    monkeypatch.delenv("AIG_ACTOR_KEY_FILE", raising=False)


def _invoke(runner: CliRunner, args: list[str]):
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    return result


def test_managed_boards_isolate_domains_route_once_and_restore_selection():
    runner = CliRunner()
    with runner.isolated_filesystem():
        root = Path.cwd()
        red = json.loads(_invoke(runner, ["board", "create", "red", "--json"]).output)[
            "data"
        ]
        blue = json.loads(
            _invoke(runner, ["board", "create", "blue", "--json"]).output
        )["data"]
        assert red["domain_id"] != blue["domain_id"]
        assert Path(red["workspace"]).is_dir()
        assert Path(blue["workspace"]).is_dir()
        assert Path.cwd() == root
        resumed = json.loads(
            _invoke(runner, ["board", "create", "red", "--resume", "--json"]).output
        )
        assert resumed["data"]["domain_id"] == red["domain_id"]
        assert (
            json.loads(_invoke(runner, ["board", "list", "--json"]).output)["data"][
                "selected"
            ]
            is None
        )

        red_asset = json.loads(
            _invoke(
                runner,
                [
                    "--board",
                    "red",
                    "asset",
                    "add",
                    "--name",
                    "shared-name",
                    "--content",
                    "red-only",
                    "--json",
                ],
            ).output
        )
        blue_asset = json.loads(
            _invoke(
                runner,
                [
                    "--board",
                    "blue",
                    "asset",
                    "add",
                    "--name",
                    "shared-name",
                    "--content",
                    "blue-only",
                    "--json",
                ],
            ).output
        )
        assert red_asset["id"] != blue_asset["id"]
        red_task = json.loads(
            _invoke(
                runner,
                [
                    "--board",
                    "red",
                    "contract",
                    "add",
                    "--name",
                    "shared-task",
                    "--output",
                    "report",
                    "--json",
                ],
            ).output
        )
        blue_task = json.loads(
            _invoke(
                runner,
                [
                    "--board",
                    "blue",
                    "contract",
                    "add",
                    "--name",
                    "shared-task",
                    "--output",
                    "summary",
                    "--json",
                ],
            ).output
        )
        assert red_task["id"] != blue_task["id"]
        red_contracts = json.loads(
            _invoke(runner, ["--board", "red", "contract", "ls", "--json"]).output
        )
        blue_contracts = json.loads(
            _invoke(runner, ["--board", "blue", "contract", "ls", "--json"]).output
        )
        assert [row["id"] for row in red_contracts] == [red_task["id"]]
        assert [row["id"] for row in blue_contracts] == [blue_task["id"]]

        shown_red = json.loads(
            _invoke(
                runner,
                ["--board", "red", "asset", "show", "shared-name", "--json"],
            ).output
        )
        shown_blue = json.loads(
            _invoke(
                runner,
                ["--board", "blue", "asset", "show", "shared-name", "--json"],
            ).output
        )
        assert shown_red["content"] == "red-only"
        assert shown_blue["content"] == "blue-only"
        red_domain = json.loads(
            _invoke(runner, ["--board", "red", "domain", "show", "--json"]).output
        )
        blue_domain = json.loads(
            _invoke(runner, ["--board", "blue", "domain", "show", "--json"]).output
        )
        assert red_domain["domain_id"] == red["domain_id"]
        assert blue_domain["domain_id"] == blue["domain_id"]
        assert (
            json.loads(_invoke(runner, ["board", "list", "--json"]).output)["data"][
                "selected"
            ]
            is None
        )
        assert Path.cwd() == root

        _invoke(runner, ["board", "switch", "red"])
        archived = json.loads(
            _invoke(runner, ["board", "archive", "red", "--json"]).output
        )["data"]
        assert archived["archived"] is True
        assert (
            json.loads(_invoke(runner, ["board", "list", "--json"]).output)["data"][
                "selected"
            ]
            == "red"
        )
        blocked = runner.invoke(cli, ["asset", "ls", "--json"])
        assert blocked.exit_code != 0
        assert "archived" in blocked.output

        restored = json.loads(
            _invoke(runner, ["board", "archive", "red", "--restore", "--json"]).output
        )["data"]
        assert restored["archived"] is False
        assert json.loads(_invoke(runner, ["asset", "ls", "--json"]).output) == [
            {
                "id": red_asset["id"],
                "name": "shared-name",
                "trust_tier": "human",
                "origin": "human",
            }
        ]


def test_explicit_initialized_workspace_can_be_adopted_without_new_genesis(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem():
        root = Path.cwd()
        external = tmp_path / "existing-workspace"
        external.mkdir()
        initialized = json.loads(
            _invoke(
                runner,
                ["-C", str(external), "domain", "init", "--json"],
            ).output
        )
        adopted = json.loads(
            _invoke(
                runner,
                [
                    "board",
                    "create",
                    "adopted",
                    "--workspace",
                    str(external),
                    "--json",
                ],
            ).output
        )["data"]
        assert adopted["domain_id"] == initialized["domain_id"]
        assert adopted["workspace"] == str(external.resolve())
        resumed = json.loads(
            _invoke(
                runner,
                [
                    "board",
                    "create",
                    "adopted",
                    "--workspace",
                    str(external),
                    "--resume",
                    "--json",
                ],
            ).output
        )
        assert resumed["data"]["domain_id"] == initialized["domain_id"]
        unrelated = tmp_path / "unrelated-managed-target"
        unrelated.mkdir()
        (Path(".aig") / "boards").mkdir(parents=True, exist_ok=True)
        (Path(".aig") / "boards" / "adopted").symlink_to(
            unrelated, target_is_directory=True
        )
        resumed_external = json.loads(
            _invoke(
                runner,
                [
                    "board",
                    "create",
                    "adopted",
                    "--workspace",
                    str(external),
                    "--resume",
                    "--json",
                ],
            ).output
        )
        assert resumed_external["data"]["domain_id"] == initialized["domain_id"]
        assert Path.cwd() == root

        duplicate = runner.invoke(
            cli,
            [
                "board",
                "create",
                "other",
                "--workspace",
                str(external),
                "--json",
            ],
        )
        assert duplicate.exit_code != 0
        assert "already registered" in duplicate.output


def test_explicit_workspace_bypasses_selection_and_board_option_conflicts():
    runner = CliRunner()
    with runner.isolated_filesystem():
        board = json.loads(
            _invoke(runner, ["board", "create", "one", "--json"]).output
        )["data"]
        _invoke(runner, ["board", "switch", "one"])
        result = _invoke(
            runner,
            ["-C", board["workspace"], "domain", "show", "--json"],
        )
        assert json.loads(result.output)["domain_id"] == board["domain_id"]
        assert Path.cwd() != Path(board["workspace"])

        conflict = runner.invoke(
            cli,
            ["--board", "one", "-C", board["workspace"], "domain", "show"],
        )
        assert conflict.exit_code != 0
        assert "mutually exclusive" in conflict.output


def test_managed_board_does_not_override_explicit_external_key_path(
    tmp_path, monkeypatch
):
    runner = CliRunner()
    external_key = tmp_path / "operator-key.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(external_key))
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["board", "create", "guarded", "--json"])
        assert result.exit_code != 0
        assert "AIG_ACTOR_KEY_FILE points outside" in result.output
        assert not Path(".aig/boards/guarded/.aig/store.db").exists()
        assert not external_key.exists()


def test_board_create_json_reports_recoverable_sync_pending(monkeypatch):
    import aigineering.local_boards as boards
    from aigineering.runtime_services import RedisSyncPending

    def pending(*_args, **_kwargs):
        raise RedisSyncPending(accepted=None)

    monkeypatch.setattr(boards, "sync_runtime", pending)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["board", "create", "pending", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"] == "board_setup_pending"
        assert payload["phase"] == "redis_sync"
        assert payload["registry_registered"] is False
        assert payload["facts_retained"] is True
        assert payload["retry_command"] == "aig board create pending"
        assert Path(payload["workspace"], ".aig", "store.db").is_file()


def test_default_managed_retry_refuses_unrelated_existing_genesis():
    runner = CliRunner()
    with runner.isolated_filesystem():
        workspace = Path(".aig/boards/guarded")
        workspace.mkdir(parents=True)
        initialized = _invoke(
            runner,
            ["-C", str(workspace), "domain", "init", "--domain", "unrelated", "--json"],
        )
        assert json.loads(initialized.output)["domain_id"]
        result = runner.invoke(cli, ["board", "create", "guarded", "--json"])
        assert result.exit_code != 0
        assert "does not match its requested name" in result.output
        assert not (Path(".aig/boards.json")).exists()
