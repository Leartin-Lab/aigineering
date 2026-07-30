"""CLI tests for durable Candidate domain initialization."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

import aigineering.local_identity as local_identity
from aigineering.cli.main import cli
from aigineering.core.signing import Ed25519Signer


def test_domain_init_creates_private_key_and_public_genesis():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["domain", "init", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        key_path = Path(data["key_file"])
        assert key_path.exists()
        if os.name != "nt":
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are unavailable")
def test_actor_key_rejects_broad_posix_permissions(tmp_path):
    path = tmp_path / "actor.ed25519"
    local_identity.write_actor_key(path, Ed25519Signer())
    path.chmod(0o644)

    with pytest.raises(ValueError, match="permissions are too broad"):
        local_identity.load_actor_signer(path)


def test_actor_key_load_bypasses_posix_mode_check_when_unsupported(
    tmp_path, monkeypatch
):
    signer = Ed25519Signer()
    path = tmp_path / "actor.ed25519"
    local_identity.write_actor_key(path, signer)
    path.chmod(0o644)
    monkeypatch.setattr(local_identity, "_posix_private_modes_supported", lambda: False)

    loaded = local_identity.load_actor_signer(path)

    assert loaded.signer_id == signer.signer_id


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
