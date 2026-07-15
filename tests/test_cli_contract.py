"""Tests for the CLI aig contract add/ls/show/run commands."""

import json
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli


def _add_contract(
    runner: CliRunner,
    name: str,
    *,
    inputs: tuple[str, ...] = (),
    outputs: tuple[str, ...] = (),
    activation: str = "",
    budget: int = 5,
    labels: tuple[str, ...] = (),
    tool_scope: tuple[str, ...] = (),
    as_json: bool = False,
):
    """Helper to invoke contract add with common options."""
    if not Path(".aig/identity/root.ed25519").exists():
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
    args = ["contract", "add", "--name", name]
    for inp in inputs:
        args.extend(["--input", inp])
    for out in outputs:
        args.extend(["--output", out])
    if activation:
        args.extend(["--activation", activation])
    if budget != 5:
        args.extend(["--budget", str(budget)])
    for label in labels:
        args.extend(["--label", label])
    for tool in tool_scope:
        args.extend(["--tool", tool])
    if as_json:
        args.append("--json")
    return runner.invoke(cli, args)


class TestContractAdd:
    """Tests for aig contract add."""

    def test_add_basic(self):
        """aig contract add --name test_ct succeeds."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _add_contract(runner, "test_ct")
            assert result.exit_code == 0, result.output
            assert "Contract injected: test_ct" in result.output

    def test_add_with_protected_output_fails(self):
        """aig contract add with --output _sys_secret is rejected."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _add_contract(runner, "bad_ct", outputs=("_sys_secret",))
            assert result.exit_code != 0
            assert "protected" in result.output.lower()

    def test_add_with_inputs_and_outputs(self):
        """aig contract add with --input and --output options succeeds."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _add_contract(
                runner,
                "multi_ct",
                inputs=("data_file", "citation_db"),
                outputs=("final_report",),
                activation="data_file AND citation_db",
            )
            assert result.exit_code == 0, result.output
            assert "Contract injected: multi_ct" in result.output

    def test_add_json_output(self):
        """aig contract add --json returns valid JSON."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _add_contract(runner, "json_ct", as_json=True)
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["name"] == "json_ct"
            assert "id" in data
            assert isinstance(data["id"], str) and len(data["id"]) > 32

    def test_add_with_labels_and_tool_scope(self):
        """aig contract add with --label and --tool options."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _add_contract(
                runner,
                "scoped_ct",
                labels=("urgent", "pii"),
                tool_scope=("search", "read"),
            )
            assert result.exit_code == 0, result.output
            assert "Contract injected: scoped_ct" in result.output


class TestContractList:
    """Tests for aig contract ls."""

    def test_ls_empty(self):
        """aig contract ls on empty store shows no contracts."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["contract", "ls"])
            assert result.exit_code == 0
            assert "No contracts found" in result.output

    def test_ls_after_add(self):
        """aig contract ls shows injected contracts."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _add_contract(runner, "alpha")
            _add_contract(runner, "beta")
            result = runner.invoke(cli, ["contract", "ls"])
            assert result.exit_code == 0
            assert "alpha" in result.output
            assert "beta" in result.output

    def test_ls_json_output(self):
        """aig contract ls --json returns valid JSON array."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _add_contract(runner, "x")
            result = runner.invoke(cli, ["contract", "ls", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["name"] == "x"
            assert "id" in data[0]


class TestContractShow:
    """Tests for aig contract show."""

    def test_show_existing_contract(self):
        """aig contract show <id> displays metadata."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            add_result = _add_contract(
                runner,
                "show_ct",
                inputs=("in_a",),
                outputs=("out_a",),
            )
            assert add_result.exit_code == 0
            # Extract contract ID from output
            cid = add_result.output.strip().split()[-1]  # last token is "(<id>)"
            cid = cid.strip("()")

            result = runner.invoke(cli, ["contract", "show", cid])
            assert result.exit_code == 0
            assert "name:       show_ct" in result.output
            assert "inputs:     ['in_a']" in result.output
            assert "outputs:    ['out_a']" in result.output
            assert "budget:     5" in result.output

    def test_show_nonexistent_fails(self):
        """aig contract show <missing> fails with an error."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["contract", "show", "nonexistent123"],
            )
            assert result.exit_code != 0
            assert "No contract with id" in result.output

    def test_show_json_output(self):
        """aig contract show <id> --json returns valid JSON."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            add_result = _add_contract(
                runner,
                "json_show_ct",
                inputs=("in",),
                outputs=("out",),
                labels=("l1",),
                tool_scope=("t1",),
            )
            assert add_result.exit_code == 0
            cid = add_result.output.strip().split()[-1].strip("()")

            result = runner.invoke(cli, ["contract", "show", cid, "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["name"] == "json_show_ct"
            assert data["id"] == cid
            assert data["inputs"] == ["in"]
            assert data["outputs"] == ["out"]
            assert data["activation"] == ""
            assert data["budget"] == 5
            assert data["labels"] == ["l1"]
            assert data["tool_scope"] == ["t1"]


class TestContractRun:
    """Tests for aig contract run."""

    def test_contract_run_is_deprecated(self):
        """aig contract run is not the agent-facing execution path."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["contract", "run", "contract_x"])
            assert result.exit_code != 0
            assert "deprecated" in result.output
