"""Behavior tests for the explicit, backup-first SQLite schema upgrade."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from aigineering.cli.domain import domain_group
from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.sqlite_migrations import (
    CURRENT_SCHEMA_VERSION,
    current_schema_version,
)
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain, load_actor_signer
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.runtime_upgrade import upgrade_local_store


def _published_store(tmp_path: Path, monkeypatch) -> tuple[Path, tuple[str, ...]]:
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    key_path = database.parent / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    store = SQLiteStore(database)
    genesis = ensure_local_domain(store)
    root_key = genesis.root_keys[0]
    signer = load_actor_signer(key_path)
    asset = build_control_plane_asset(name="upgrade-evidence", content="signed source")
    assert publish_effect(
        store,
        store,
        genesis,
        root_key,
        signer,
        asset_proposal_effect(asset),
        idempotency_key="upgrade-test-asset",
    ).accepted
    contract = build_control_plane_contract(
        name="upgrade-task",
        description="A signed Contract preserved through schema migration.",
        inputs=("upgrade-evidence",),
        outputs=("upgrade-result",),
        budget=3,
    )
    assert publish_effect(
        store,
        store,
        genesis,
        root_key,
        signer,
        contract_declaration_effect(contract),
        idempotency_key="upgrade-test-contract",
    ).accepted
    original = tuple(record.id for _revision, record in store.scan_runtime_records())
    store.close()
    return database, original


def _set_schema_version(database: Path, version: int) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE schema_version SET version = ?", (version,))
        connection.commit()
    finally:
        connection.close()


def _schema_version(path: Path) -> int:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return current_schema_version(connection)
    finally:
        connection.close()


def test_upgrade_preserves_signed_facts_and_current_rerun_is_noop(
    tmp_path, monkeypatch
):
    database, original_record_ids = _published_store(tmp_path, monkeypatch)
    before = SQLiteStore.snapshot_from_path(database)
    assets_before = before.get_all_assets()
    contracts_before = before.get_all_contracts()
    revision_before = before.get_runtime_revision()
    before.close()
    old_version = CURRENT_SCHEMA_VERSION - 1
    _set_schema_version(database, old_version)

    result = upgrade_local_store(database)
    assert result["from_version"] == old_version
    assert result["to_version"] == CURRENT_SCHEMA_VERSION
    backup_path = Path(str(result["backup"]))
    assert backup_path.is_file()
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
    assert _schema_version(backup_path) == old_version

    after = SQLiteStore.snapshot_from_path(database)
    assert after.get_all_assets() == assets_before
    assert after.get_all_contracts() == contracts_before
    assert (
        tuple(record.id for _rev, record in after.scan_runtime_records())
        == original_record_ids
    )
    assert after.get_runtime_revision() == revision_before
    after.close()

    noop = upgrade_local_store(database)
    assert noop == {
        "from_version": CURRENT_SCHEMA_VERSION,
        "to_version": CURRENT_SCHEMA_VERSION,
        "backup": None,
        "revision": revision_before,
    }


def test_missing_and_future_schema_fail_before_creating_backup_or_store(tmp_path):
    missing = tmp_path / "missing" / ".aig" / "store.db"
    with pytest.raises(FileNotFoundError):
        upgrade_local_store(missing)
    assert not missing.exists()
    assert not missing.parent.exists()

    database = tmp_path / "future" / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    store = SQLiteStore(database)
    store.close()
    _set_schema_version(database, CURRENT_SCHEMA_VERSION + 1)
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="newer than supported"):
        upgrade_local_store(database)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    assert not (database.parent / "backups").exists()


def test_failed_migration_keeps_source_version_and_completed_backup(
    tmp_path, monkeypatch
):
    from aigineering.core.sqlite_migrations import SQLiteMigrator

    database, _records = _published_store(tmp_path, monkeypatch)
    old_version = CURRENT_SCHEMA_VERSION - 1
    _set_schema_version(database, old_version)
    migration_name = f"_migrate_to_v{CURRENT_SCHEMA_VERSION}"
    original_migration = getattr(SQLiteMigrator, migration_name)

    def fail_migration(self):
        raise RuntimeError("fixture migration failure")

    monkeypatch.setattr(SQLiteMigrator, migration_name, fail_migration)
    with pytest.raises(RuntimeError, match="fixture migration failure"):
        upgrade_local_store(database)
    assert _schema_version(database) == old_version
    backups = tuple((database.parent / "backups").glob(f"schema-{old_version}-*.db"))
    assert len(backups) == 1
    assert _schema_version(backups[0]) == old_version
    assert getattr(SQLiteMigrator, migration_name) is not original_migration


def test_domain_upgrade_help_and_json_result(tmp_path, monkeypatch):
    database, _records = _published_store(tmp_path, monkeypatch)
    _set_schema_version(database, CURRENT_SCHEMA_VERSION - 1)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    help_result = runner.invoke(domain_group, ["upgrade", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "existing local SQLite Store" in help_result.output

    result = runner.invoke(domain_group, ["upgrade", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["from_version"] == CURRENT_SCHEMA_VERSION - 1
    assert payload["data"]["to_version"] == CURRENT_SCHEMA_VERSION
    assert Path(payload["data"]["backup"]).is_file()
