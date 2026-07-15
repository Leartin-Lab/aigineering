"""CLI tests for durable Candidate domain initialization."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli


def test_domain_init_creates_private_key_and_public_genesis():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["domain", "init", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        key_path = Path(data["key_file"])
        assert key_path.exists()
        assert key_path.stat().st_mode & 0o077 == 0
        private_key = key_path.read_text(encoding="ascii").strip()
        assert len(private_key) == 64
        assert private_key not in result.output

        shown = runner.invoke(cli, ["domain", "show", "--json"])
        assert shown.exit_code == 0, shown.output
        public = json.loads(shown.output)
        assert public["domain_id"] == data["domain_id"]
        assert public["root_keys"][0]["public_key"] == data["public_key"]
        assert private_key not in shown.output


def test_domain_cannot_be_initialized_twice():
    runner = CliRunner()
    with runner.isolated_filesystem():
        assert runner.invoke(cli, ["domain", "init"]).exit_code == 0

        repeated = runner.invoke(cli, ["domain", "init"])

        assert repeated.exit_code != 0
        assert "already initialized" in repeated.output


def test_contract_add_requires_initialized_domain():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["contract", "add", "--name", "root"])

        assert result.exit_code != 0
        assert "not been initialized" in result.output


def test_contract_add_records_candidate_commitment():
    runner = CliRunner()
    with runner.isolated_filesystem():
        assert runner.invoke(cli, ["domain", "init"]).exit_code == 0
        added = runner.invoke(
            cli,
            ["contract", "add", "--name", "root", "--output", "report"],
        )
        assert added.exit_code == 0, added.output

        from aigineering.cli._common import _persistent_store

        store = _persistent_store()
        record_types = {
            record.record_type for _, record in store.scan_runtime_records()
        }
        assert "candidate.received" in record_types
        assert "candidate.committed" in record_types
        assert "contract.declared" in record_types
        store.close()


def test_contract_add_rejects_private_key_not_in_genesis():
    runner = CliRunner()
    with runner.isolated_filesystem():
        assert runner.invoke(cli, ["domain", "init"]).exit_code == 0
        from aigineering.core.signing import Ed25519Signer

        Path(".aig/identity/root.ed25519").write_text(
            Ed25519Signer().private_key_hex + "\n", encoding="ascii"
        )
        result = runner.invoke(cli, ["contract", "add", "--name", "forged"])

        assert result.exit_code != 0
        assert "not authorized" in result.output
