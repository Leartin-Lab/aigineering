"""Backup-first reconstruction evidence, operating only on private copies."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys

from aigineering import __version__
from aigineering.core.sqlite_migrations import CURRENT_SCHEMA_VERSION
from aigineering.core.sqlite_store import SQLiteStore


class UnsupportedDiagnosticSchema(ValueError):
    """Reconstruction evidence must not silently include a schema migration."""


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _backup(source: Path, target: Path) -> None:
    # SQLite backup includes committed WAL data, unlike copying the main file.
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with (
        closing(_readonly(source)) as reader,
        closing(sqlite3.connect(target)) as writer,
    ):
        reader.backup(writer)


def _table_fingerprints(path: Path) -> dict:
    """Order-independent row fingerprints; never export row contents."""
    result = {}
    with closing(_readonly(path)) as connection:
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for (name,) in names:
            identifier = '"' + name.replace('"', '""') + '"'
            hashes = []
            for row in connection.execute(f"SELECT * FROM {identifier}"):
                values = [
                    {"blob_hex": value.hex()} if isinstance(value, bytes) else value
                    for value in row
                ]
                encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
                hashes.append(hashlib.sha256(encoded.encode()).hexdigest())
            digest = hashlib.sha256("".join(sorted(hashes)).encode()).hexdigest()
            result[name] = {"rows": len(hashes), "sha256": digest}
    return result


def _write_manifest(directory: Path, report: dict) -> None:
    temporary = directory / "manifest.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(report, stream, sort_keys=True, indent=2)
        stream.write("\n")
    temporary.replace(directory / "manifest.json")


def verify_reconstruction(source: Path, output_directory: Path) -> dict:
    """Preserve a consistent snapshot, rebuild a second copy, retain evidence.

    A mismatch or exception is a non-passing result, never a repair of *source*.
    The caller must keep the entire output directory private: database copies
    contain the original data, including sealed values. The manifest contains
    only counts, fingerprints, version information, and error categories.
    """
    source = Path(source).resolve(strict=True)
    if not source.is_file():
        raise ValueError("source must be an existing SQLite file")
    directory = Path(output_directory)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    before = directory / "before.sqlite"
    after = directory / "rebuilt.sqlite"
    report = {
        "format_version": 1,
        "package_version": __version__,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.system(),
        "status": "incomplete",
        "snapshots": [before.name, after.name],
    }
    _write_manifest(directory, report)
    try:
        _backup(source, before)
        report["before_tables"] = _table_fingerprints(before)
        with closing(_readonly(before)) as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
        report["schema_version"] = version
        if version != CURRENT_SCHEMA_VERSION:
            raise UnsupportedDiagnosticSchema(
                "use the matching runtime for this schema"
            )
        _backup(before, after)
        store = SQLiteStore(str(after))
        try:
            report["before_digest"] = store.runtime_materialization_digest()
            store.rebuild_runtime_materializations()
            report["after_digest"] = store.runtime_materialization_digest()
        finally:
            store.close()
        report["after_tables"] = _table_fingerprints(after)
        report["records_unchanged"] = report["before_tables"].get(
            "runtime_records"
        ) == report["after_tables"].get("runtime_records")
        report["status"] = (
            "passed"
            if report["before_digest"] == report["after_digest"]
            and report["records_unchanged"]
            else "mismatch"
        )
    except Exception as error:
        # Error text can contain raw record content. Preserve the category only;
        # the untouched snapshot permits local diagnosis without exporting it.
        report["status"] = "error"
        report["error_type"] = type(error).__name__
        if after.exists():
            try:
                report["after_tables"] = _table_fingerprints(after)
            except sqlite3.Error:
                pass
    if "after_tables" in report:
        report["changed_tables"] = sorted(
            name
            for name in report["before_tables"].keys() | report["after_tables"].keys()
            if report["before_tables"].get(name) != report["after_tables"].get(name)
        )
    _write_manifest(directory, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="new private directory for full database copies and manifest",
    )
    args = parser.parse_args(argv)
    try:
        report = verify_reconstruction(args.database, args.output_dir)
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "error", "error_type": type(error).__name__}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
