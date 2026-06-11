"""Tests for typed canonical hashing (v0.3.1b).

Verifies:
  - Key-order stability in canonical JSON
  - Unicode NFC normalization
  - Typed hash domain prefixes (task:, asset:, def:, lineage:, event:, claim:)
  - Deterministic contract and asset identity
  - Hash sensitivity to field changes
"""

import json
import unicodedata

from aigineering.core.ids import (
    canonical_json,
    compute_content_hash,
    hash_content,
    hash_contract,
    hash_asset_content,
    hash_asset_definition,
    hash_lineage,
    hash_event,
    hash_claim,
    asset_id,
    contract_id,
    trace_entry_id,
)


# ─────────────────────────────────────────────────────────────────────────────
# Key-order stability
# ─────────────────────────────────────────────────────────────────────────────


def test_key_order_stability_identical_hash():
    """Two JSON objects with different key-insertion order produce the same hash."""
    obj_a = {"name": "task1", "budget": 5, "activation": "x AND y"}
    obj_b = {"activation": "x AND y", "budget": 5, "name": "task1"}
    a = canonical_json(obj_a)
    b = canonical_json(obj_b)
    assert a == b
    assert compute_content_hash(a) == compute_content_hash(b)


def test_key_order_stability_via_asset_id():
    """asset_id() produces identical results regardless of key order in the
    input canonical JSON."""
    c1 = json.dumps({"name": "a", "content": "b"}, sort_keys=True)
    c2 = json.dumps({"content": "b", "name": "a"}, sort_keys=True)
    assert asset_id(c1) == asset_id(c2)


def test_key_order_stability_via_contract_id():
    """contract_id() produces identical results regardless of key order."""
    base = {
        "name": "test", "description": "", "inputs": ["i"],
        "outputs": ["o"], "activation": "", "budget": 1,
        "tool_scope": [], "labels": [], "origin": "human",
    }
    c1 = json.dumps(base, sort_keys=True)
    # Reverse key order dict (Python 3.7+ preserves insertion order)
    reversed_base = dict(reversed(list(base.items())))
    c2 = json.dumps(reversed_base)
    assert contract_id(c1) == contract_id(c2)


# ─────────────────────────────────────────────────────────────────────────────
# Unicode NFC normalization
# ─────────────────────────────────────────────────────────────────────────────


def test_unicode_nfc_normalization_precomposed_vs_decomposed():
    """é (U+00E9) and e + combining acute (U+0065+U+0301) produce the same hash."""
    precomposed = "\u00E9"           # é (single codepoint)
    decomposed = "e\u0301"           # e + combining acute (two codepoints)

    assert precomposed != decomposed  # different byte sequences
    assert unicodedata.normalize("NFC", precomposed) == unicodedata.normalize("NFC", decomposed)
    assert compute_content_hash(precomposed) == compute_content_hash(decomposed)


def test_unicode_nfc_normalization_stable_for_ascii():
    """ASCII content is unaffected by NFC normalization."""
    assert compute_content_hash("hello world") == compute_content_hash("hello world")
    assert len(compute_content_hash("hello")) == 64


def test_unicode_nfc_normalization_in_asset_content():
    """hash_asset_content normalizes NFC before hashing."""
    precomposed_name = "r\u00E9sum\u00E9"
    decomposed_name = "re\u0301sume\u0301"
    assert hash_asset_content(precomposed_name, "data") == hash_asset_content(decomposed_name, "data")


def test_unicode_nfc_normalization_in_contract():
    """hash_contract normalizes all string fields via NFC."""
    a = hash_contract(
        name="caf\u00E9", description="desc", inputs=["in"],
        outputs=["out"], activation="act", budget=1,
        tool_scope=[], labels=[], origin="human",
    )
    b = hash_contract(
        name="cafe\u0301", description="desc", inputs=["in"],
        outputs=["out"], activation="act", budget=1,
        tool_scope=[], labels=[], origin="human",
    )
    assert a == b


# ─────────────────────────────────────────────────────────────────────────────
# Typed hash domains (prefix verification)
# ─────────────────────────────────────────────────────────────────────────────


def test_typed_hash_domains_contract():
    cid = hash_contract("task1", "desc", ["in1"], ["out1"], "act", 5, [], [], "human")
    assert cid.startswith("task:")
    assert len(cid) == 5 + 64  # "task:" + 64 hex chars


def test_typed_hash_domains_asset_content():
    aid = hash_asset_content("report", "content")
    assert aid.startswith("asset:")
    assert len(aid) == 6 + 64  # "asset:" + 64 hex chars


def test_typed_hash_domains_asset_definition():
    did = hash_asset_definition("my_asset")
    assert did.startswith("def:")
    assert len(did) == 4 + 64  # "def:" + 64 hex chars


def test_typed_hash_domains_lineage():
    lid = hash_lineage("group1", ["a", "b"])
    assert lid.startswith("lineage:")
    assert len(lid) == 8 + 64  # "lineage:" + 64 hex chars


def test_typed_hash_domains_event():
    eid = hash_event("contract_1", "activation", 0)
    assert eid.startswith("event:")
    assert len(eid) == 6 + 64  # "event:" + 64 hex chars


def test_typed_hash_domains_claim():
    cid = hash_claim("src_id", "repl_id", "override")
    assert cid.startswith("claim:")
    assert len(cid) == 6 + 64  # "claim:" + 64 hex chars


def test_typed_hash_domains_are_distinct():
    """Different domain tags never collide."""
    c = hash_contract("same", "same", ["same"], ["same"], "same", 1, [], [], "human")
    a = hash_asset_content("same", "same")
    d = hash_asset_definition("same")
    l = hash_lineage("same", ["same"])
    e = hash_event("same", "same", 0)
    cl = hash_claim("same", "same", "same")
    ids = {c, a, d, l, e, cl}
    assert len(ids) == 6, "all domain hashes should be distinct"


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic contract identity
# ─────────────────────────────────────────────────────────────────────────────


def test_deterministic_contract_id_same_inputs():
    """Same inputs always produce the same contract hash."""
    a = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    b = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert a == b


def test_deterministic_contract_id_ordering_invariant():
    """Input/output/tool_scope/labels ordering does not affect hash."""
    a = hash_contract("n", "d", ["b", "a"], ["y", "x"], "a", 1, ["t2", "t1"], ["l2", "l1"], "human")
    b = hash_contract("n", "d", ["a", "b"], ["x", "y"], "a", 1, ["t1", "t2"], ["l1", "l2"], "human")
    assert a == b


def test_deterministic_asset_id_same_inputs():
    """Same name+content always produce the same asset hash."""
    a = hash_asset_content("report", "final content")
    b = hash_asset_content("report", "final content")
    assert a == b


# ─────────────────────────────────────────────────────────────────────────────
# Hash includes all fields
# ─────────────────────────────────────────────────────────────────────────────


def test_hash_includes_all_fields_name():
    ref = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert hash_contract("n2", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human") != ref


def test_hash_includes_all_fields_description():
    ref = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert hash_contract("n", "d2", ["i"], ["o"], "a", 1, ["t"], ["l"], "human") != ref


def test_hash_includes_all_fields_inputs():
    ref = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert hash_contract("n", "d", ["i2"], ["o"], "a", 1, ["t"], ["l"], "human") != ref


def test_hash_includes_all_fields_outputs():
    ref = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert hash_contract("n", "d", ["i"], ["o2"], "a", 1, ["t"], ["l"], "human") != ref


def test_hash_includes_all_fields_activation():
    ref = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert hash_contract("n", "d", ["i"], ["o"], "a2", 1, ["t"], ["l"], "human") != ref


def test_hash_includes_all_fields_budget():
    ref = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert hash_contract("n", "d", ["i"], ["o"], "a", 2, ["t"], ["l"], "human") != ref


def test_hash_includes_all_fields_tool_scope():
    ref = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t2"], ["l"], "human") != ref


def test_hash_includes_all_fields_labels():
    ref = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l2"], "human") != ref


def test_hash_includes_all_fields_origin():
    ref = hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert hash_contract("n", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "system") != ref


# ─────────────────────────────────────────────────────────────────────────────
# Event hash domain
# ─────────────────────────────────────────────────────────────────────────────


def test_trace_entry_id_uses_event_prefix():
    """trace_entry_id() now uses the event: prefix via hash_event."""
    tid = trace_entry_id("c1", "activation", 0)
    assert tid.startswith("event:")


def test_trace_entry_id_parent_id_optional():
    """Omitting parent_id is equivalent to parent_id=None."""
    t1 = trace_entry_id("c1", "ev", 0)
    t2 = trace_entry_id("c1", "ev", 0, parent_id=None)
    assert t1 == t2


# ─────────────────────────────────────────────────────────────────────────────
# Backward-compat wrappers
# ─────────────────────────────────────────────────────────────────────────────


def test_hash_content_backward_compat():
    """hash_content is an alias for compute_content_hash."""
    assert hash_content("test") == compute_content_hash("test")


def test_contract_id_wrapper_from_json():
    """contract_id() wrapper parses JSON and delegates to hash_contract."""
    canonical = json.dumps(
        {"name": "test", "description": "d", "inputs": ["i"],
         "outputs": ["o"], "activation": "a", "budget": 1,
         "tool_scope": ["t"], "labels": ["l"], "origin": "human"},
        sort_keys=True,
    )
    wid = contract_id(canonical)
    direct = hash_contract("test", "d", ["i"], ["o"], "a", 1, ["t"], ["l"], "human")
    assert wid == direct


def test_asset_id_wrapper_from_json():
    """asset_id() wrapper parses JSON and delegates to hash_asset_content."""
    canonical = json.dumps({"name": "n", "content": "c"}, sort_keys=True)
    wid = asset_id(canonical)
    direct = hash_asset_content("n", "c")
    assert wid == direct
