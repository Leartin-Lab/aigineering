"""Tests for persisted trace replay consistency."""

import json
import hashlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.replay import replay_session
from aigineering.core.session import SessionStore
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import JsonLStore
from aigineering.core.provenance import sign_asset, verify_asset_seal
from aigineering.protocol.types import Asset
from aigineering.protocol.types import Session, TraceEntry
from aigineering.protocol.wire import trace_entry_to_dict


def _write_trace(path: Path, entries: list[TraceEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(trace_entry_to_dict(entry)) + "\n")


@pytest.mark.parametrize("session_id", ["../escape", "session_../../escape", "a/b"])
def test_session_paths_reject_traversal(tmp_path, session_id):
    store = SessionStore(str(tmp_path / "sessions"))

    with pytest.raises(ValueError, match="invalid session id"):
        store.get_session(session_id)
    result = replay_session(
        session_id,
        sessions_dir=str(tmp_path / "sessions"),
        traces_dir=str(tmp_path / "traces"),
        store_dir=str(tmp_path / "store"),
        sqlite_path=str(tmp_path / "missing.sqlite"),
    )
    assert result["consistent"] is False
    assert result["error_type"] == "ValueError"


def test_replay_error_has_stable_read_model_shape(tmp_path):
    result = replay_session(
        "session_missing",
        sessions_dir=str(tmp_path / "sessions"),
        traces_dir=str(tmp_path / "traces"),
        store_dir=str(tmp_path / "store"),
        sqlite_path=str(tmp_path / "missing.sqlite"),
    )

    assert result["entries"] == []
    assert result["by_event"] == {}
    assert result["accepted_count"] == result["rejected_count"] == 0
    assert result["consistent"] is False


def test_malformed_trace_fails_closed_without_echoing_raw_line(tmp_path):
    sessions_dir = tmp_path / "sessions"
    traces_dir = tmp_path / "traces"
    session = Session(
        id="session_broken",
        root_contract_id="contract_root",
        trace_ids=["trace_expected"],
    )
    SessionStore(str(sessions_dir)).create_session(session)
    trace_path = traces_dir / "session_broken.jsonl"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text('{"payload":"PRIVATE_TRACE_SENTINEL_724"', encoding="utf-8")

    result = replay_session(
        "session_broken",
        sessions_dir=str(sessions_dir),
        traces_dir=str(traces_dir),
        store_dir=str(tmp_path / "store"),
        sqlite_path=str(tmp_path / "missing.sqlite"),
    )

    assert result["consistent"] is False
    assert result["error_type"] == "JSONDecodeError"
    assert "PRIVATE_TRACE_SENTINEL_724" not in str(result)

    runner = CliRunner()
    with runner.isolated_filesystem():
        session_dir = Path(".aig/sessions")
        session_dir.mkdir(parents=True)
        (session_dir / "session_broken.json").write_text(
            json.dumps(
                {
                    "id": "session_broken",
                    "root_contract_id": "contract_root",
                    "trace_ids": ["trace_expected"],
                }
            ),
            encoding="utf-8",
        )
        trace_dir = Path(".aig/traces")
        trace_dir.mkdir(parents=True)
        (trace_dir / "session_broken.jsonl").write_text(
            '{"payload":"PRIVATE_TRACE_SENTINEL_724"', encoding="utf-8"
        )
        cli_result = runner.invoke(cli, ["replay", "session_broken", "--json"])
        assert cli_result.exit_code == 1
        assert "JSONDecodeError" in cli_result.output
        assert "PRIVATE_TRACE_SENTINEL_724" not in cli_result.output


def test_replay_counts_tool_executed_assets(tmp_path):
    sessions_dir = tmp_path / "sessions"
    traces_dir = tmp_path / "traces"
    entry = TraceEntry(
        id="trace_tool",
        contract_id="contract_tool",
        event_type="tool_executed",
        accepted_fragments=["asset_call", "asset_obs"],
        accepted_asset_names=["_tool_call_x", "_tool_obs_x"],
    )
    session = Session(
        id="session_test",
        root_contract_id="contract_root",
        trace_ids=[entry.id],
    )

    SessionStore(str(sessions_dir)).create_session(session)
    _write_trace(traces_dir / "session_test.jsonl", [entry])

    result = replay_session(
        "session_test",
        sessions_dir=str(sessions_dir),
        traces_dir=str(traces_dir),
        store_dir=str(tmp_path / "store"),
        sqlite_path=str(tmp_path / "missing.sqlite"),
    )

    assert result["accepted_count"] == 2
    assert result["rejected_count"] == 0
    assert result["consistent"] is False
    assert "sqlite_runtime_records_and_materializations" in result["not_checked"]


def test_replay_detects_duplicate_tool_assets(tmp_path):
    sessions_dir = tmp_path / "sessions"
    traces_dir = tmp_path / "traces"
    entries = [
        TraceEntry(
            id="trace_tool_1",
            contract_id="contract_tool_1",
            event_type="tool_executed",
            accepted_fragments=["asset_obs"],
        ),
        TraceEntry(
            id="trace_tool_2",
            contract_id="contract_tool_2",
            event_type="tool_executed",
            accepted_fragments=["asset_obs"],
        ),
    ]
    session = Session(
        id="session_test",
        root_contract_id="contract_root",
        trace_ids=[entry.id for entry in entries],
    )

    SessionStore(str(sessions_dir)).create_session(session)
    _write_trace(traces_dir / "session_test.jsonl", entries)

    result = replay_session(
        "session_test",
        sessions_dir=str(sessions_dir),
        traces_dir=str(traces_dir),
        store_dir=str(tmp_path / "store"),
        sqlite_path=str(tmp_path / "missing.sqlite"),
    )

    assert result["consistent"] is False
    assert result["duplicate_ids"] == ["asset_obs"]


def test_replay_compares_manifest_and_jsonl_to_sqlite_trace(tmp_path):
    sessions_dir = tmp_path / "sessions"
    traces_dir = tmp_path / "traces"
    sqlite_path = tmp_path / "runtime.sqlite"
    entry = TraceEntry(
        id="trace_sqlite",
        contract_id="task_sqlite",
        event_type="candidate_committed",
        accepted_fragments=['{"candidate_id":"c1","effect_type":"asset.propose"}'],
        accepted_asset_names=["report"],
    )
    store = SQLiteStore(str(sqlite_path))
    store.append_trace_entry(entry)
    store.close()
    before_digest = hashlib.sha256(sqlite_path.read_bytes()).hexdigest()
    session = Session(
        id="session_test",
        root_contract_id="task_sqlite",
        trace_ids=[entry.id],
    )
    SessionStore(str(sessions_dir)).create_session(session)
    _write_trace(traces_dir / "session_test.jsonl", [entry])

    result = replay_session(
        "session_test",
        sessions_dir=str(sessions_dir),
        traces_dir=str(traces_dir),
        store_dir=str(tmp_path / "store"),
        sqlite_path=str(sqlite_path),
    )

    assert result["consistent"] is True
    assert result["candidate_decision_count"] == 1
    assert result["effect_count"] == 1
    assert result["accepted_output_count"] == 1
    assert result["accepted_count"] == 0
    assert result["trace_provenance"][entry.id]["revision"] > 0
    assert result["trace_provenance"][entry.id]["recorded_at"]
    assert hashlib.sha256(sqlite_path.read_bytes()).hexdigest() == before_digest

    tampered_entry = TraceEntry(
        id=entry.id,
        contract_id=entry.contract_id,
        event_type="candidate_committed",
        accepted_fragments=entry.accepted_fragments,
        accepted_asset_names=["tampered"],
    )
    _write_trace(traces_dir / "session_test.jsonl", [tampered_entry])
    changed = replay_session(
        "session_test",
        sessions_dir=str(sessions_dir),
        traces_dir=str(traces_dir),
        store_dir=str(tmp_path / "store"),
        sqlite_path=str(sqlite_path),
    )
    assert changed["consistent"] is False
    assert changed["trace_mismatches"] == [entry.id]

    extra = TraceEntry(id="trace_extra", contract_id="task_sqlite", event_type="note")
    _write_trace(traces_dir / "session_test.jsonl", [entry, extra])
    injected = replay_session(
        "session_test",
        sessions_dir=str(sessions_dir),
        traces_dir=str(traces_dir),
        store_dir=str(tmp_path / "store"),
        sqlite_path=str(sqlite_path),
    )
    assert injected["consistent"] is False
    assert injected["manifest_trace_mismatch"]["extra"] == [extra.id]

    _write_trace(traces_dir / "session_test.jsonl", [extra])
    deleted = replay_session(
        "session_test",
        sessions_dir=str(sessions_dir),
        traces_dir=str(traces_dir),
        store_dir=str(tmp_path / "store"),
        sqlite_path=str(sqlite_path),
    )
    assert deleted["consistent"] is False
    assert deleted["manifest_trace_mismatch"]["missing"] == [entry.id]

    _write_trace(traces_dir / "session_test.jsonl", [entry])
    malformed_db = tmp_path / "malformed.sqlite"
    malformed_db.write_text("PRIVATE_DB_SENTINEL", encoding="utf-8")
    bad_database = replay_session(
        "session_test",
        sessions_dir=str(sessions_dir),
        traces_dir=str(traces_dir),
        store_dir=str(tmp_path / "store"),
        sqlite_path=str(malformed_db),
    )
    assert bad_database["consistent"] is False
    assert bad_database["integrity"]["error_type"] in {"ValueError", "DatabaseError"}
    assert "PRIVATE_DB_SENTINEL" not in str(bad_database)


def test_replay_flags_accepted_asset_missing_from_sqlite(tmp_path):
    sessions_dir = tmp_path / "sessions"
    traces_dir = tmp_path / "traces"
    sqlite_path = tmp_path / "runtime.sqlite"
    entry = TraceEntry(
        id="trace_projection",
        contract_id="task_sqlite",
        event_type="projection",
        accepted_fragments=["asset:missing"],
    )
    store = SQLiteStore(str(sqlite_path))
    store.append_trace_entry(entry)
    store.close()
    session = Session(
        id="session_missing_asset",
        root_contract_id="task_sqlite",
        trace_ids=[entry.id],
    )
    SessionStore(str(sessions_dir)).create_session(session)
    _write_trace(traces_dir / "session_missing_asset.jsonl", [entry])

    result = replay_session(
        session.id,
        sessions_dir=str(sessions_dir),
        traces_dir=str(traces_dir),
        store_dir=str(tmp_path / "store"),
        sqlite_path=str(sqlite_path),
    )

    assert result["consistent"] is False
    assert result["missing_asset_ids"] == ["asset:missing"]


def test_replay_detects_asset_signature_mismatch(tmp_path):
    sessions_dir = tmp_path / "sessions"
    traces_dir = tmp_path / "traces"
    store_dir = tmp_path / "store"
    signed = sign_asset(
        Asset(
            id="asset_report",
            name="report",
            content="ok",
            created_by="contract_root",
            minted_by="worker",
        )
    )
    tampered = Asset(
        id=signed.id,
        name=signed.name,
        content=signed.content,
        created_by=signed.created_by,
        minted_by=signed.minted_by,
        source_uri="tampered://source",
        signed_by=signed.signed_by,
        provenance_seal=signed.provenance_seal,
    )
    entry = TraceEntry(
        id="trace_projection",
        contract_id="contract_root",
        event_type="projection",
        accepted_fragments=[signed.id],
        accepted_asset_names=["report"],
    )
    session = Session(
        id="session_test",
        root_contract_id="contract_root",
        trace_ids=[entry.id],
    )

    SessionStore(str(sessions_dir)).create_session(session)
    _write_trace(traces_dir / "session_test.jsonl", [entry])
    # Store rejects tampered assets at write time (G3 enforcement)
    assert not verify_asset_seal(tampered), (
        "Tampered asset must fail signature verification"
    )
    with pytest.raises(ValueError, match="missing or invalid canonical seal"):
        JsonLStore(
            str(store_dir / "assets.jsonl"),
            str(store_dir / "contracts.jsonl"),
        ).add_asset(tampered)
