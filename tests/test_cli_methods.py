"""CLI coverage for reusable method packages and lifecycle commands."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.cli._candidate import commit_local_effects, require_accepted
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.effect_builders import asset_proposal_effect


def _package() -> dict:
    return {
        "schema": "method-package-v1",
        "name": "check-number",
        "version": "1.0.0",
        "description": "Check two numbers.",
        "instructions": "Compare source and reported values.",
        "inputs": ["source"],
        "outputs": {"assessment": {"matched": "boolean"}},
        "requirements": {
            field: []
            for field in (
                "tool_scope",
                "worker_capabilities",
                "worker_pools",
                "delegation_capabilities",
                "delegation_pools",
            )
        },
        "dependencies": [],
        "context_asset_ids": [],
        "examples": [],
    }


def _cases() -> dict:
    return {
        "schema": "method-cases-v1",
        "name": "held-out",
        "cases": [
            {
                "name": "same",
                "inputs": {"source": '{"source": 1, "reported": 1}'},
                "expected": {"assessment": {"matched": True}},
            }
        ],
    }


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _initialized_runner(tmp_path, monkeypatch) -> CliRunner:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["domain", "init", "--json"])
    assert result.exit_code == 0, result.output
    return runner


def _import_method(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(cli, ["method", "import", str(path), "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["asset_id"]


def _source(runner: CliRunner) -> str:
    result = runner.invoke(
        cli,
        ["asset", "add", "--name", "source", "--content", "input", "--json"],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["id"]


def test_method_import_cases_and_show_json(tmp_path, monkeypatch):
    runner = _initialized_runner(tmp_path, monkeypatch)
    package_path = _write_json(tmp_path / "package.json", _package())
    cases_path = _write_json(tmp_path / "cases.json", _cases())

    method_id = _import_method(runner, package_path)
    cases = runner.invoke(
        cli, ["method", "cases", str(cases_path), "--name", "heldout", "--json"]
    )
    assert cases.exit_code == 0, cases.output
    cases_id = json.loads(cases.output)["asset_id"]
    shown = runner.invoke(cli, ["method", "show", method_id, "--json"])
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.output)
    assert payload["asset_id"] == method_id
    assert json.loads(payload["content"])["schema"] == "method-package-v1"
    assert cases_id != method_id


def test_method_file_and_binding_errors_are_click_errors(tmp_path, monkeypatch):
    runner = _initialized_runner(tmp_path, monkeypatch)
    missing = runner.invoke(cli, ["method", "import", "missing.json", "--json"])
    assert missing.exit_code != 0

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (256 * 1024 + 1))
    result = runner.invoke(cli, ["method", "import", str(oversized), "--json"])
    assert result.exit_code != 0
    assert "256 KiB" in result.output

    method_id = _import_method(
        runner, _write_json(tmp_path / "package.json", _package())
    )
    duplicate = runner.invoke(
        cli,
        [
            "method",
            "instantiate",
            method_id,
            "--input",
            "source=a",
            "--input",
            "source=b",
            "--output",
            "assessment=result",
            "--name",
            "duplicate",
            "--budget",
            "1",
            "--allow-unverified",
            "--json",
        ],
    )
    assert duplicate.exit_code != 0
    assert "duplicate" in duplicate.output


def test_method_instantiate_requires_evaluation_unless_explicitly_unverified(
    tmp_path, monkeypatch
):
    runner = _initialized_runner(tmp_path, monkeypatch)
    method_id = _import_method(
        runner, _write_json(tmp_path / "package.json", _package())
    )
    source_id = _source(runner)
    args = [
        "method",
        "instantiate",
        method_id,
        "--input",
        f"source={source_id}",
        "--output",
        "assessment=result",
        "--name",
        "trial",
        "--budget",
        "1",
        "--json",
    ]
    rejected = runner.invoke(cli, args)
    assert rejected.exit_code != 0
    assert "evaluation" in rejected.output

    accepted = runner.invoke(cli, [*args, "--allow-unverified"])
    assert accepted.exit_code == 0, accepted.output
    contract_id = json.loads(accepted.output)["contract_id"]
    store = SQLiteStore(str(tmp_path / ".aig" / "store.db"))
    try:
        contract = store.get_contract(contract_id)
        assert contract is not None
        assert contract.id == contract_id
        assert contract.outputs == ("result",)
    finally:
        store.close()


def test_method_create_returns_an_ordinary_contract(tmp_path, monkeypatch):
    runner = _initialized_runner(tmp_path, monkeypatch)
    request_id = _source(runner)
    result = runner.invoke(
        cli,
        [
            "method",
            "create",
            "--request",
            request_id,
            "--output",
            "generated_method",
            "--name",
            "author",
            "--budget",
            "2",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    contract_id = json.loads(result.output)["contract_id"]
    store = SQLiteStore(str(tmp_path / ".aig" / "store.db"))
    try:
        assert store.get_contract(contract_id) is not None
    finally:
        store.close()


def test_method_test_only_prepares_run_and_assess_rejects_unfinished_run(
    tmp_path, monkeypatch
):
    runner = _initialized_runner(tmp_path, monkeypatch)
    method_id = _import_method(
        runner, _write_json(tmp_path / "package.json", _package())
    )
    cases_path = _write_json(tmp_path / "cases.json", _cases())
    cases_result = runner.invoke(
        cli, ["method", "cases", str(cases_path), "--name", "heldout", "--json"]
    )
    assert cases_result.exit_code == 0, cases_result.output
    suite_id = json.loads(cases_result.output)["asset_id"]
    prepared = runner.invoke(
        cli,
        [
            "method",
            "test",
            method_id,
            "--cases",
            suite_id,
            "--name",
            "run",
            "--budget",
            "1",
            "--json",
        ],
    )
    assert prepared.exit_code == 0, prepared.output
    prepared_payload = json.loads(prepared.output)
    assert prepared_payload["run_id"].startswith("asset:")
    assert prepared_payload["contract_id"].startswith("task:")
    assessed = runner.invoke(
        cli, ["method", "assess", prepared_payload["run_id"], "--json"]
    )
    assert assessed.exit_code != 0
    assert "terminal" in assessed.output


def test_method_show_rejects_signed_redacted_and_nonpromptable_assets(
    tmp_path, monkeypatch
):
    runner = _initialized_runner(tmp_path, monkeypatch)
    store = SQLiteStore(str(tmp_path / ".aig" / "store.db"))
    try:
        redacted = replace(
            build_control_plane_asset(
                name="redacted-method", content="redacted-secret"
            ),
            disclosure_view="redacted",
        )
        sealed = build_control_plane_asset(
            name="sealed-method", content="sealed-secret", promptable=False
        )
        redacted_asset = require_accepted(
            commit_local_effects(
                store,
                (asset_proposal_effect(redacted),),
                idempotency_key=f"test:{redacted.id}",
            )
        ).assets[0]
        sealed_asset = require_accepted(
            commit_local_effects(
                store,
                (asset_proposal_effect(sealed),),
                idempotency_key=f"test:{sealed.id}",
            )
        ).assets[0]
    finally:
        store.close()

    for asset in (redacted_asset, sealed_asset):
        shown = runner.invoke(cli, ["method", "show", asset.id, "--json"])
        assert shown.exit_code != 0
        assert "secret" not in shown.output
