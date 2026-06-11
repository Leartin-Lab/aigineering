"""Planner capability containment tests (v0.3.2)."""

import json

import pytest

from aigineering.core.ids import hash_contract
from aigineering.core.methods import contracts_from_plan_asset
from aigineering.protocol.types import Asset, Contract


def _plan_asset(contracts: list[dict]) -> Asset:
    payload = json.dumps({"contracts": contracts}, sort_keys=True)
    return Asset(
        id="asset_plan",
        name="_plan_result_parent",
        content=payload,
    )


def _basic_child(**overrides: object) -> dict:
    child: dict = {
        "name": "draft",
        "description": "Draft the report.",
        "inputs": ["source"],
        "outputs": ["draft_report"],
        "activation": "source",
        "budget": 2,
        "tool_scope": ["read"],
        "labels": ["user"],
    }
    child.update({k: v for k, v in overrides.items() if v is not None})
    return child


def _parent(**overrides: object) -> Contract:
    fields: dict = {
        "id": "parent_1",
        "name": "root",
        "budget": 5,
        "tool_scope": ["read"],
        "labels": ["user"],
    }
    fields.update({k: v for k, v in overrides.items() if v is not None})
    return Contract(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Deny-by-default: tool-scope escalation → clamped
# ---------------------------------------------------------------------------


def test_planner_tool_scope_escalation_clamped():
    """Parent has tool_scope=['read']; planner emits ['read','write'] → clamped."""
    parent = _parent(tool_scope=["read"])
    asset = _plan_asset([_basic_child(tool_scope=["read", "write"])])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0]["field"] == "tool_scope"
    assert rejected[0]["action"] == "clamped"
    assert accepted[0].tool_scope == ("read",)


def test_planner_tool_scope_no_escalation_allowed():
    """Parent has no tool scope; planner emits non-empty scope → all tools clamped."""
    parent = _parent(tool_scope=[])
    asset = _plan_asset([_basic_child(tool_scope=["write"])])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0]["action"] == "clamped"
    assert accepted[0].tool_scope == ()


# ---------------------------------------------------------------------------
# Deny-by-default: protected output prefix → rejected
# ---------------------------------------------------------------------------


def test_planner_reserved_output_rejected():
    """Planner emits output '_sys_hack' → rejected."""
    parent = _parent()
    asset = _plan_asset([_basic_child(outputs=["_sys_hack"])])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert rejected[0]["field"] == "outputs"
    assert rejected[0]["action"] == "rejected"
    assert "_sys_hack" in rejected[0]["actual"]


@pytest.mark.parametrize("prefix", ["_sys_", "_skill_", "_memory_", "_mcp_", "_soul_", "_persona_"])
def test_every_reserved_output_prefix_blocked(prefix):
    parent = _parent()
    output_name = f"{prefix}forbidden"
    asset = _plan_asset([_basic_child(outputs=[output_name])])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert rejected[0]["action"] == "rejected"


# ---------------------------------------------------------------------------
# Origin hard-clamped to "plan"
# ---------------------------------------------------------------------------


def test_planner_system_origin_clamped():
    """Planner sets origin='system' in raw dict → engine clamps to 'plan'."""
    parent = _parent()
    # Planner includes "origin": "system" in the raw dict.
    # origin is NOT in _PLAN_PROTECTED_FIELDS so the child is accepted,
    # but the engine always sets origin="plan".
    child_raw = _basic_child()
    child_raw["origin"] = "system"
    asset = _plan_asset([child_raw])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 1
    assert len(rejected) == 0
    assert accepted[0].origin == "plan"


# ---------------------------------------------------------------------------
# Label laundering → rejected
# ---------------------------------------------------------------------------


def test_label_laundering_blocked():
    """Parent labels=['user']; planner emits ['admin'] → rejected."""
    parent = _parent(labels=["user"])
    asset = _plan_asset([_basic_child(labels=["admin"])])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert rejected[0]["field"] == "labels"
    assert rejected[0]["action"] == "rejected"


def test_label_superset_blocked():
    """Parent labels=['user']; planner emits ['user', 'admin'] → rejected."""
    parent = _parent(labels=["user"])
    asset = _plan_asset([_basic_child(labels=["user", "admin"])])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert rejected[0]["action"] == "rejected"


# ---------------------------------------------------------------------------
# Budget fan-out → clamped
# ---------------------------------------------------------------------------


def test_budget_fanout_bounded():
    """Planner requests budget=100, parent budget=5 → clamped to 5."""
    parent = _parent(budget=5)
    asset = _plan_asset([_basic_child(budget=100)])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0]["field"] == "budget"
    assert rejected[0]["action"] == "clamped"
    assert accepted[0].budget == 5


def test_budget_within_bounds_accepted():
    """Planner requests budget=3, parent budget=5 → accepted as-is."""
    parent = _parent(budget=5)
    asset = _plan_asset([_basic_child(budget=3)])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 1
    assert len(rejected) == 0
    assert accepted[0].budget == 3


# ---------------------------------------------------------------------------
# Protected fields → rejected
# ---------------------------------------------------------------------------


def test_planner_cannot_set_minting_authority():
    """Planner tries to set minting_authority → rejected."""
    parent = _parent()
    child_raw = _basic_child()
    child_raw["minting_authority"] = "self"
    asset = _plan_asset([child_raw])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert "minting_authority" in rejected[0]["field"]
    assert rejected[0]["action"] == "rejected"


@pytest.mark.parametrize("field", ["trust_tier", "created_by"])
def test_planner_cannot_set_each_protected_field(field):
    parent = _parent()
    child_raw = _basic_child()
    child_raw[field] = "system"
    asset = _plan_asset([child_raw])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert field in rejected[0]["field"]
    assert rejected[0]["action"] == "rejected"


# ---------------------------------------------------------------------------
# Valid expansion (regression)
# ---------------------------------------------------------------------------


def test_valid_plan_expansion_still_works():
    """Planner within bounds → expansion succeeds."""
    parent = _parent()
    asset = _plan_asset([_basic_child()])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 1
    assert len(rejected) == 0
    c = accepted[0]
    assert c.name == "draft"
    assert c.origin == "plan"
    assert c.parent_id == parent.id
    assert c.tool_scope == ("read",)
    assert c.labels == ("user",)
    assert c.budget == 2
    assert c.outputs == ("draft_report",)


# ---------------------------------------------------------------------------
# Backward compatibility (no parent_contract)
# ---------------------------------------------------------------------------


def test_no_parent_contract_passes_all_through():
    """Without parent_contract, all children are accepted (backward compat)."""
    asset = _plan_asset([_basic_child(tool_scope=["read", "write"], labels=["admin"])])
    accepted, rejected = contracts_from_plan_asset(asset, "parent_1")

    assert len(accepted) == 1
    assert len(rejected) == 0
    assert accepted[0].tool_scope == ("read", "write")


# ---------------------------------------------------------------------------
# tool_scope_hash participation in contract identity
# ---------------------------------------------------------------------------


def test_tool_scope_changes_contract_id():
    """Changing tool_scope produces a different contract ID."""
    id_a = hash_contract(
        name="x", description="", inputs=[], outputs=[],
        activation="", budget=1, tool_scope=["read"],
        labels=[], origin="plan",
    )
    id_b = hash_contract(
        name="x", description="", inputs=[], outputs=[],
        activation="", budget=1, tool_scope=["read", "write"],
        labels=[], origin="plan",
    )
    assert id_a != id_b


def test_tool_scope_clamping_changes_identity():
    """When tool_scope is clamped, the contract ID incorporates the clamped scope."""
    parent = _parent(tool_scope=["read"])
    asset = _plan_asset([_basic_child(tool_scope=["read", "write"])])
    accepted, _ = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    clamped_id = accepted[0].id
    unclamped_id = hash_contract(
        name="draft", description="Draft the report.",
        inputs=["source"], outputs=["draft_report"],
        activation="source", budget=2, tool_scope=["read", "write"],
        labels=["user"], origin="plan",
    )
    assert clamped_id != unclamped_id


# ---------------------------------------------------------------------------
# Multiple children mixed outcomes
# ---------------------------------------------------------------------------


def test_mixed_valid_and_rejected_children():
    """Some children pass, some are rejected — each handled independently."""
    parent = _parent()
    asset = _plan_asset([
        _basic_child(name="good", labels=["user"]),
        _basic_child(name="bad_labels", labels=["admin"]),
        _basic_child(name="bad_output", outputs=["_sys_key"]),
        _basic_child(name="good2", tool_scope=["read"]),
    ])
    accepted, rejected = contracts_from_plan_asset(
        asset, parent.id, parent_contract=parent,
    )

    assert len(accepted) == 2
    accepted_names = {c.name for c in accepted}
    assert accepted_names == {"good", "good2"}

    assert len(rejected) == 2
    rejected_names = {r["child_name"] for r in rejected}
    assert rejected_names == {"bad_labels", "bad_output"}
