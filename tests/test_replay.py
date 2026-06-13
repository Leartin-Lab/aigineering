"""Tests for persisted trace replay consistency."""

import json
from pathlib import Path

import pytest

from aigineering.core.replay import replay_session
from aigineering.core.session import SessionStore
from aigineering.core.store import JsonLStore
from aigineering.core.provenance import sign_asset, verify_asset_signature
from aigineering.protocol.types import Asset
from aigineering.protocol.types import Session, TraceEntry
from aigineering.protocol.wire import trace_entry_to_dict


def _write_trace(path: Path, entries: list[TraceEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(trace_entry_to_dict(entry)) + "\n")


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
    )

    assert result["accepted_count"] == 2
    assert result["rejected_count"] == 0
    assert result["consistent"] is True


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
    )

    assert result["consistent"] is False
    assert result["duplicate_ids"] == ["asset_obs"]


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
        signature=signed.signature,
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
    assert not verify_asset_signature(tampered), (
        "G3/N-P1.6: Tampered asset must fail signature verification"
    )
    with pytest.raises(ValueError, match="missing or invalid canonical seal"):
        JsonLStore(
            str(store_dir / "assets.jsonl"),
            str(store_dir / "contracts.jsonl"),
        ).add_asset(tampered)
