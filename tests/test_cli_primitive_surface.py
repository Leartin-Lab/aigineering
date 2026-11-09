from __future__ import annotations

import json
import os

from click.testing import CliRunner

from aigineering.cli.main import cli


def test_task_help_exposes_readers_and_core_lifecycle_only() -> None:
    task = cli.commands["task"]
    removed = {
        "handoff",
        "edit",
        "assign",
        "reassign",
        "link",
        "unlink",
        "block",
        "unblock",
        "promote",
        "priority",
        "comment",
    }
    assert removed.isdisjoint(task.commands)
    assert {
        "assignments",
        "attachment",
        "comments",
        "dependencies",
        "edits",
        "handoffs",
        "holds",
        "priorities",
        "promotions",
        "revisions",
        "create",
        "cancel",
        "retry",
    } <= task.commands.keys()

    runner = CliRunner()
    task_help = runner.invoke(cli, ["task", "--help"])
    assert task_help.exit_code == 0
    assert "cancel" in task_help.output
    assert "retry" in task_help.output
    for name in removed:
        assert f"  {name} " not in task_help.output

    for command in ("list", "board"):
        result = runner.invoke(cli, ["task", command, "--help"])
        assert result.exit_code == 0
        assert "--sort" not in result.output
        assert "priority" not in result.output

    attachment_help = runner.invoke(cli, ["task", "attachment", "--help"])
    assert attachment_help.exit_code == 0
    assert "list" in attachment_help.output and "export" in attachment_help.output
    assert "add" not in attachment_help.output


def test_normal_task_creation_asset_publication_and_signed_cancel(tmp_path) -> None:
    redis_url = os.getenv("AIG_REDIS_TEST_URL", "redis://127.0.0.1:32773/4")
    env = {
        "AIGINEERING_REDIS_URL": redis_url,
        "AIG_REDIS_TEST_URL": redis_url,
    }
    runner = CliRunner()

    def invoke(args: list[str]):
        result = runner.invoke(cli, ["-C", str(tmp_path), *args], env=env)
        assert result.exit_code == 0, result.output
        return result

    invoke(["domain", "init", "--json"])
    asset_result = invoke(
        ["asset", "add", "--name", "input-note", "--content", "evidence", "--json"]
    )
    assert json.loads(asset_result.output)["name"] == "input-note"
    create_result = invoke(
        [
            "task",
            "create",
            "--name",
            "primitive task",
            "--input",
            "input-note",
            "--json",
        ]
    )
    created = json.loads(create_result.output)
    assert created["ok"] is True
    assert created["ok"] is True
    contract_id = created["contract_id"]

    cancel_result = invoke(
        ["task", "cancel", contract_id, "--reason", "operator stop", "--json"]
    )
    cancelled = json.loads(cancel_result.output)
    assert cancelled["ok"] is True
    assert contract_id in str(cancelled["data"])
