"""Explicit schema upgrade service for an existing local SQLite Store."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path

from aigineering.core.sqlite_migrations import (
    CURRENT_SCHEMA_VERSION,
    current_schema_version,
)
from aigineering.core.sqlite_store import SQLiteStore


def _readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _read_schema_version(path: Path) -> int:
    connection = _readonly_connection(path)
    try:
        try:
            return current_schema_version(connection)
        except sqlite3.Error as exc:
            raise ValueError(
                "SQLite file has no readable Aigineering schema version"
            ) from exc
    finally:
        connection.close()


def _read_runtime_revision(path: Path) -> int:
    connection = _readonly_connection(path)
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM runtime_records"
        ).fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.Error as exc:
        raise ValueError(
            "current SQLite schema is missing runtime revision data"
        ) from exc
    finally:
        connection.close()


def _create_backup(source_path: Path, backup_dir: Path, schema_version: int) -> Path:
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    base_time = time.time_ns()
    while True:
        backup_path = backup_dir / f"schema-{schema_version}-{base_time}.db"
        try:
            descriptor = os.open(
                backup_path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600,
            )
        except FileExistsError:
            base_time += 1
            continue
        os.close(descriptor)
        break

    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = _readonly_connection(source_path)
        destination = sqlite3.connect(backup_path)
        source.backup(destination)
        destination.commit()
        os.chmod(backup_path, 0o600)
        return backup_path
    except Exception:
        if destination is not None:
            destination.close()
            destination = None
        try:
            backup_path.unlink()
        except OSError:
            pass
        raise
    finally:
        if source is not None:
            source.close()
        if destination is not None:
            destination.close()


def upgrade_local_store(
    db_path: str | Path = ".aig/store.db",
    *,
    backup_dir: str | Path | None = None,
) -> dict[str, int | str | None]:
    """Back up and migrate an existing regular Store without resetting facts.

    Future, missing, malformed and unsupported Stores are rejected before a
    backup is created or a writable Store is opened. A migration error leaves
    the completed pre-migration backup available for recovery.
    """
    path = Path(db_path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise FileNotFoundError(f"SQLite Store does not exist at {path}") from None
    if not stat.S_ISREG(mode):
        raise ValueError(f"SQLite Store path is not a regular file: {path}")

    from_version = _read_schema_version(path)
    if from_version < 1:
        raise ValueError("SQLite file does not contain a supported initialized Store")
    if from_version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"SQLite schema version {from_version} is newer than supported version "
            f"{CURRENT_SCHEMA_VERSION}; refusing to upgrade"
        )
    if from_version == CURRENT_SCHEMA_VERSION:
        return {
            "from_version": from_version,
            "to_version": CURRENT_SCHEMA_VERSION,
            "backup": None,
            "revision": _read_runtime_revision(path),
        }

    selected_backup_dir = (
        Path(backup_dir) if backup_dir is not None else path.parent / "backups"
    )
    backup_path = _create_backup(path, selected_backup_dir, from_version)
    store = SQLiteStore(path)
    try:
        to_version = store.schema_version
        if to_version != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"SQLite migration stopped at schema {to_version}; expected "
                f"{CURRENT_SCHEMA_VERSION}"
            )
        revision = store.get_runtime_revision()
    finally:
        store.close()
    return {
        "from_version": from_version,
        "to_version": to_version,
        "backup": str(backup_path),
        "revision": revision,
    }
