#!/usr/bin/env python3
"""Produce a bounded, value-redacted profile of one authorized CSV or TSV."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import stat
import sys
from typing import Any

SCHEMA_VERSION = "scientific-tabular-profile-v1"
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
HARD_MAX_BYTES = 512 * 1024 * 1024
MAX_DISTINCT_TRACKED = 10_000


class ProfileError(ValueError):
    """A safe, user-facing table validation error."""


def authorized_file(input_name: str, *, root: Path, max_bytes: int) -> Path:
    """Resolve one regular non-linked file below an explicit root."""
    relative = Path(input_name)
    if relative.is_absolute() or ".." in relative.parts or "~" in relative.parts:
        raise ProfileError("input must be a relative path without traversal")
    root = root.resolve(strict=True)
    candidate = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ProfileError("symbolic links are not accepted")
    resolved = candidate.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise ProfileError("input resolves outside the authorized root")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ProfileError("input must be a regular file")
    if info.st_nlink != 1:
        raise ProfileError("multiply linked inputs are not accepted")
    if info.st_size > max_bytes:
        raise ProfileError(f"input exceeds the configured {max_bytes} byte limit")
    return resolved


def profile_table(
    path: Path,
    *,
    max_rows: int,
    max_columns: int,
    missing_tokens: tuple[str, ...],
    reveal_field_names: bool,
) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix not in {".csv", ".tsv"}:
        raise ProfileError("only .csv and .tsv inputs are supported")
    delimiter = "," if suffix == ".csv" else "\t"
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream, delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ProfileError("table must contain a header") from exc
        if not header or len(header) > max_columns:
            raise ProfileError("table has no columns or exceeds the column limit")
        header = [name.strip() for name in header]
        if len(set(header)) != len(header) or any(not name for name in header):
            raise ProfileError("header names must be non-empty and unique")
        columns = [
            {
                "missing": 0,
                "non_missing": 0,
                "distinct": set(),
                "distinct_truncated": False,
                "types": set(),
            }
            for _ in header
        ]
        scanned_rows = 0
        truncated = False
        for row in reader:
            if scanned_rows >= max_rows:
                truncated = True
                break
            if len(row) != len(header):
                raise ProfileError(
                    f"row {scanned_rows + 2} has {len(row)} fields; expected {len(header)}"
                )
            scanned_rows += 1
            for state, value in zip(columns, row, strict=True):
                if value in missing_tokens:
                    state["missing"] += 1
                    continue
                state["non_missing"] += 1
                if len(state["distinct"]) < MAX_DISTINCT_TRACKED:
                    state["distinct"].add(value)
                elif value not in state["distinct"]:
                    state["distinct_truncated"] = True
                state["types"].add(_scalar_type(value))

    return {
        "schema_version": SCHEMA_VERSION,
        "input_sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "format": suffix.removeprefix("."),
        "delimiter": "comma" if delimiter == "," else "tab",
        "encoding": "utf-8",
        "missing_tokens": list(missing_tokens),
        "field_names_revealed": reveal_field_names,
        "scanned_rows": scanned_rows,
        "column_count": len(header),
        "truncated": truncated,
        "columns": [
            {
                "position": index + 1,
                "field": name if reveal_field_names else f"field_{index + 1:03d}",
                "missing_count": state["missing"],
                "non_missing_count": state["non_missing"],
                "distinct_count": len(state["distinct"]),
                "distinct_count_truncated": state["distinct_truncated"],
                "inferred_type": _combined_type(state["types"]),
            }
            for index, (name, state) in enumerate(zip(header, columns, strict=True))
        ],
    }


def _scalar_type(value: str) -> str:
    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return "boolean"
    try:
        int(value)
        return "integer"
    except ValueError:
        pass
    try:
        float(value)
        return "number"
    except ValueError:
        return "text"


def _combined_type(types: set[str]) -> str:
    if not types:
        return "empty"
    if types <= {"integer"}:
        return "integer"
    if types <= {"integer", "number"}:
        return "number"
    if len(types) == 1:
        return next(iter(types))
    return "mixed"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, default=100_000)
    parser.add_argument("--max-columns", type=int, default=512)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--missing-token", action="append", default=[""])
    parser.add_argument("--reveal-field-names", action="store_true")
    parser.add_argument("--action", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 1 <= args.max_rows <= 1_000_000:
            raise ProfileError("max-rows must be between 1 and 1000000")
        if not 1 <= args.max_columns <= 4096:
            raise ProfileError("max-columns must be between 1 and 4096")
        if not 1 <= args.max_bytes <= HARD_MAX_BYTES:
            raise ProfileError(f"max-bytes must be between 1 and {HARD_MAX_BYTES}")
        path = authorized_file(args.input, root=args.root, max_bytes=args.max_bytes)
        document = json.dumps(
            profile_table(
                path,
                max_rows=args.max_rows,
                max_columns=args.max_columns,
                missing_tokens=tuple(dict.fromkeys(args.missing_token)),
                reveal_field_names=args.reveal_field_names,
            ),
            sort_keys=True,
            ensure_ascii=False,
        )
        if args.action:
            document = "/exec " + json.dumps(
                {"outputs": {"data_profile": document}},
                sort_keys=True,
                ensure_ascii=False,
            )
        print(document)
        return 0
    except (OSError, UnicodeError, csv.Error, ProfileError) as exc:
        print(f"profiling failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
