import os
from pathlib import Path
import subprocess
import sys

import pytest

from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import JsonLTraceStore
from aigineering.protocol.types import Contract
from conftest import candidate_runtime


class _FailingExportStore:
    def append(self, _entry):
        raise RuntimeError("export unavailable")


class _CommitObservingJsonLStore(JsonLTraceStore):
    def __init__(self, file_path: str, authoritative: SQLiteStore) -> None:
        super().__init__(file_path)
        self._authoritative = authoritative

    def append(self, entry):
        assert any(
            record.payload["trace"]["id"] == entry.id
            for _, record in self._authoritative.scan_runtime_records(
                record_type="trace.recorded"
            )
        )
        super().append(entry)


def test_trace_export_failure_cannot_roll_back_authoritative_commit(tmp_path):
    store = SQLiteStore(str(tmp_path / "runtime.db"))
    runtime = candidate_runtime(store, _FailingExportStore())
    contract = Contract(id="legacy:export-crash", outputs=("result",), budget=1)

    with pytest.raises(RuntimeError, match="export unavailable"):
        runtime.accept_contract(contract)

    assert len(store.get_all_contracts()) == 1
    records = store.scan_runtime_records(record_type="trace.recorded")
    assert records
    assert store.get_all()


def test_jsonl_trace_export_runs_only_after_sqlite_commit(tmp_path):
    store = SQLiteStore(str(tmp_path / "runtime.db"))
    export = _CommitObservingJsonLStore(str(tmp_path / "session.jsonl"), store)
    runtime = candidate_runtime(store, export)
    contract = Contract(id="legacy:export-order", outputs=("result",), budget=1)

    runtime.accept_contract(contract)

    authoritative = {entry.id for entry in store.get_all()}
    exported = {entry.id for entry in export.get_all()}
    assert exported
    assert exported <= authoritative


def test_jsonl_trace_serialization_is_hashseed_independent(tmp_path):
    script = """
import sys
import sqlite3
from dataclasses import replace
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import JsonLTraceStore, create_entry

metadata = {key: key for key in {"zeta", "alpha", "middle"}}
entry = replace(
    create_entry("root", "usage", usage_metadata=metadata),
    timestamp="fixed",
)
JsonLTraceStore(sys.argv[1]).append(entry)
store = SQLiteStore(sys.argv[2])
store.append_trace_entry(entry)
store.close()
with sqlite3.connect(sys.argv[2]) as connection:
    print(connection.execute(
        "SELECT usage_metadata FROM trace_events WHERE id = ?", (entry.id,)
    ).fetchone()[0])
"""
    source_path = Path(__file__).parents[1] / "src"
    outputs = []
    sqlite_values = []
    for seed in ("17", "841"):
        trace_path = tmp_path / f"trace-{seed}.jsonl"
        database_path = tmp_path / f"store-{seed}.db"
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = os.pathsep.join(
            [str(source_path), env.get("PYTHONPATH", "")]
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(trace_path), str(database_path)],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        outputs.append(trace_path.read_bytes())
        sqlite_values.append(result.stdout)

    assert outputs[0] == outputs[1]
    assert sqlite_values[0] == sqlite_values[1]
