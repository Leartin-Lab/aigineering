"""Tests for typed canonical hashing."""

import json

from aigineering.core.ids import (
    compute_content_hash,
    hash_content,
    asset_id,
    contract_id,
    trace_entry_id,
    hash_contract,
    hash_asset_content,
    hash_asset_definition,
    hash_lineage,
    hash_event,
    hash_claim,
    now_iso,
    canonical_json,
)


# ── hash_content / compute_content_hash ─────────────────────────────────


def test_hash_content_deterministic():
    assert hash_content("hello") == hash_content("hello")
    assert hash_content("hello") != hash_content("world")


def test_hash_content_format():
    h = hash_content("test")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_compute_content_hash_nfc_normalization():
    # U+00E9 (é) and U+0065+U+0301 (e + combining acute) produce same hash
    precomposed = "\u00e9"  # é
    decomposed = "e\u0301"  # e + combining acute
    assert compute_content_hash(precomposed) == compute_content_hash(decomposed)


# ── asset_id (backward-compat) ──────────────────────────────────────────


def test_asset_id():
    canonical = json.dumps({"name": "x", "content": "y"}, sort_keys=True)
    aid = asset_id(canonical)
    assert aid.startswith("content:")
    assert len(aid) == 8 + 64  # "content:" + 64 hex chars


def test_asset_id_deterministic():
    c1 = json.dumps({"name": "a", "content": "b"}, sort_keys=True)
    c2 = json.dumps({"content": "b", "name": "a"}, sort_keys=True)
    assert asset_id(c1) == asset_id(c2)


# ── contract_id (backward-compat) ───────────────────────────────────────


def test_contract_id():
    canonical = json.dumps(
        {
            "name": "test",
            "outputs": [],
            "inputs": [],
            "activation": "",
            "budget": 0,
            "description": "",
            "tool_scope": [],
            "labels": [],
            "origin": "human",
        },
        sort_keys=True,
    )
    cid = contract_id(canonical)
    assert cid.startswith("task:")
    assert len(cid) == 5 + 64  # "task:" + 64 hex chars


# ── trace_entry_id (backward-compat) ────────────────────────────────────


def test_trace_entry_id():
    tid = trace_entry_id("contract_abc", "activation", 0)
    assert tid.startswith("event:")


def test_trace_entry_id_deterministic():
    t1 = trace_entry_id("c1", "e1", 0, parent_id="p1")
    t2 = trace_entry_id("c1", "e1", 0, parent_id="p1")
    assert t1 == t2


# ── typed hash domains ──────────────────────────────────────────────────


def test_hash_contract_prefix():
    cid = hash_contract("task1", "desc", ["in1"], ["out1"], "act", 5, [], [], "human")
    assert cid.startswith("task:")


def test_hash_contract_deterministic():
    a = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    b = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert a == b


def test_hash_contract_includes_all_fields():
    ref = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    # Changing name
    assert hash_contract("n2", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human") != ref
    # Changing description
    assert hash_contract("n", "d2", ["i"], ["o"], "a", 1, ["t"], ["l"], "human") != ref
    # Changing inputs
    assert hash_contract("n", "d", ["i2"], ["o"], "a", 1, ["t"], ["l"], "human") != ref
    # Changing activation
    assert hash_contract("n", "d", ["i"], ["o"], "a2", 1, ["t"], ["l"], "human") != ref
    # Changing budget
    assert hash_contract("n", "d", ["i"], ["o"], "a", 2, ["t"], ["l"], "human") != ref
    # Changing origin
    assert hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "system") != ref


def test_hash_asset_content_prefix():
    cid = hash_asset_content("report", "content here")
    assert cid.startswith("content:")


def test_hash_asset_content_deterministic():
    a = hash_asset_content("n", "c")
    b = hash_asset_content("n", "c")
    assert a == b


def test_hash_asset_content_different_name():
    assert hash_asset_content("a", "c") != hash_asset_content("b", "c")


def test_hash_asset_content_different_content():
    assert hash_asset_content("n", "a") != hash_asset_content("n", "b")


def test_hash_asset_definition_prefix():
    did = hash_asset_definition("my_asset")
    assert did.startswith("def:")
    assert len(did) == 4 + 64  # "def:" + 64 hex chars


def test_hash_lineage_prefix():
    lid = hash_lineage("group1", ["asset_a", "asset_b"])
    assert lid.startswith("lineage:")
    assert len(lid) == 8 + 64  # "lineage:" + 64 hex chars


def test_hash_lineage_deterministic():
    a = hash_lineage("g", ["a1", "a2"])
    b = hash_lineage("g", ["a2", "a1"])  # sorted
    assert a == b


def test_hash_event_prefix():
    eid = hash_event("contract_x", "activation", 0)
    assert eid.startswith("event:")
    assert len(eid) == 6 + 64  # "event:" + 64 hex chars


def test_hash_event_deterministic():
    a = hash_event("c1", "ev", 1, parent_id="p1")
    b = hash_event("c1", "ev", 1, parent_id="p1")
    assert a == b


def test_hash_claim_prefix():
    cid = hash_claim("src", "repl", "override")
    assert cid.startswith("claim:")
    assert len(cid) == 6 + 64  # "claim:" + 64 hex chars


# ── canonical_json ──────────────────────────────────────────────────────


def test_canonical_json_key_order_stability():
    # Different key insertion order yields the same serialization
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b
    assert a == '{"a":2,"b":1}'


def test_canonical_json_no_whitespace():
    result = canonical_json({"name": "test", "value": 42})
    assert " " not in result


def test_canonical_json_preserves_unicode():
    result = canonical_json({"key": "\u00e9"})  # é
    assert "\u00e9" in result or "é" in result


# ── now_iso ─────────────────────────────────────────────────────────────


def test_now_iso():
    ts = now_iso()
    assert "T" in ts
    assert ts.endswith("+00:00") or ts.endswith("Z") or "+" in ts
