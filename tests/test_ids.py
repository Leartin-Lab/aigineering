"""Tests for SHA-256 deterministic ID generation."""

import json

from aigineering.core.ids import (
    hash_content,
    asset_id,
    contract_id,
    trace_entry_id,
    now_iso,
)


def test_hash_content_deterministic():
    assert hash_content("hello") == hash_content("hello")
    assert hash_content("hello") != hash_content("world")


def test_hash_content_format():
    h = hash_content("test")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_asset_id():
    canonical = json.dumps({"name": "x", "content": "y"}, sort_keys=True)
    aid = asset_id(canonical)
    assert aid.startswith("asset_")
    assert len(aid) == 6 + 64  # "asset_" + 64 hex chars


def test_asset_id_deterministic():
    c1 = json.dumps({"name": "a", "content": "b"}, sort_keys=True)
    c2 = json.dumps({"content": "b", "name": "a"}, sort_keys=True)
    assert asset_id(c1) == asset_id(c2)


def test_contract_id():
    canonical = json.dumps({"name": "test"}, sort_keys=True)
    cid = contract_id(canonical)
    assert cid.startswith("contract_")
    assert len(cid) == 9 + 64


def test_trace_entry_id():
    tid = trace_entry_id("contract_abc", "activation", 0)
    assert tid.startswith("trace_")


def test_trace_entry_id_deterministic():
    t1 = trace_entry_id("c1", "e1", 0, parent_id="p1")
    t2 = trace_entry_id("c1", "e1", 0, parent_id="p1")
    assert t1 == t2


def test_now_iso():
    ts = now_iso()
    assert "T" in ts
    assert ts.endswith("+00:00") or ts.endswith("Z") or "+" in ts
