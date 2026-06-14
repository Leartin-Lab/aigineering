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


def test_planner_tool_scope_escalation_rejected():
    """Parent has tool_scope=['read']; planner emits ['read','write'] → rejected."""
    parent = _parent(tool_scope=["read"])
    asset = _plan_asset([_basic_child(tool_scope=["read", "write"])])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert rejected[0]["field"] == "tool_scope"
    assert rejected[0]["action"] == "rejected"


def test_planner_tool_scope_no_escalation_allowed():
    """Parent has no tool scope; planner emits non-empty scope → rejected."""
    parent = _parent(tool_scope=[])
    asset = _plan_asset([_basic_child(tool_scope=["write"])])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert rejected[0]["action"] == "rejected"


# ---------------------------------------------------------------------------
# Deny-by-default: protected output prefix → rejected
# ---------------------------------------------------------------------------


def test_planner_reserved_output_rejected():
    """Planner emits output '_sys_hack' → rejected."""
    parent = _parent()
    asset = _plan_asset([_basic_child(outputs=["_sys_hack"])])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert rejected[0]["field"] == "outputs"
    assert rejected[0]["action"] == "rejected"
    assert "_sys_hack" in rejected[0]["actual"]


@pytest.mark.parametrize(
    "prefix", ["_sys_", "_skill_", "_memory_", "_mcp_", "_soul_", "_persona_"]
)
def test_every_reserved_output_prefix_blocked(prefix):
    parent = _parent()
    output_name = f"{prefix}forbidden"
    asset = _plan_asset([_basic_child(outputs=[output_name])])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
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
        asset,
        parent.id,
        parent_contract=parent,
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
        asset,
        parent.id,
        parent_contract=parent,
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
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert rejected[0]["action"] == "rejected"


# ---------------------------------------------------------------------------
# Budget fan-out → clamped
# ---------------------------------------------------------------------------


def test_budget_fanout_bounded():
    """Planner requests budget=100, parent budget=5 → contained to 5."""
    parent = _parent(budget=5)
    asset = _plan_asset([_basic_child(budget=100)])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0]["field"] == "budget"
    assert rejected[0]["action"] == "budget_contained"
    assert rejected[0].get("requested") == 100
    assert rejected[0].get("effective") == 5
    assert accepted[0].budget == 5


def test_budget_within_bounds_accepted():
    """Planner requests budget=3, parent budget=5 → accepted as-is."""
    parent = _parent(budget=5)
    asset = _plan_asset([_basic_child(budget=3)])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 1
    assert len(rejected) == 0
    assert accepted[0].budget == 3


# ---------------------------------------------------------------------------
# Protected fields → rejected
# ---------------------------------------------------------------------------


def test_planner_cannot_set_minting_authority():
    """Planner sets minting_authority → accepted but ignored (N-P2.14: field removed from protected set).

    minting_authority is no longer in _PLAN_PROTECTED_FIELDS — plans can include it
    but the Contract constructor default (empty tuple) still applies. The child is
    accepted with minting_authority=().
    """
    parent = _parent()
    child_raw = _basic_child()
    child_raw["minting_authority"] = "self"
    asset = _plan_asset([child_raw])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 1
    assert accepted[0].minting_authority == ()


@pytest.mark.parametrize("field", ["trust_tier", "created_by"])
def test_planner_cannot_set_each_protected_field(field):
    parent = _parent()
    child_raw = _basic_child()
    child_raw[field] = "system"
    asset = _plan_asset([child_raw])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
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
        asset,
        parent.id,
        parent_contract=parent,
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
        name="x",
        description="",
        inputs=[],
        outputs=[],
        activation="",
        budget=1,
        tool_scope=["read"],
        labels=[],
        origin="plan",
    )
    id_b = hash_contract(
        name="x",
        description="",
        inputs=[],
        outputs=[],
        activation="",
        budget=1,
        tool_scope=["read", "write"],
        labels=[],
        origin="plan",
    )
    assert id_a != id_b


def test_tool_scope_subset_accepted_with_correct_identity():
    """When tool_scope is a subset of parent scope, child is accepted with its scope."""
    parent = _parent(tool_scope=["read", "write"])
    asset = _plan_asset([_basic_child(tool_scope=["read"])])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 1
    assert len(rejected) == 0
    assert accepted[0].tool_scope == ("read",)
    expected_id = hash_contract(
        name="draft",
        description="Draft the report.",
        inputs=["source"],
        outputs=["draft_report"],
        activation="source",
        budget=2,
        tool_scope=["read"],
        labels=["user"],
        origin="plan",
    )
    assert accepted[0].id == expected_id


# ---------------------------------------------------------------------------
# Multiple children mixed outcomes
# ---------------------------------------------------------------------------


def test_mixed_valid_and_rejected_children():
    """Some children pass, some are rejected — each handled independently."""
    parent = _parent()
    asset = _plan_asset(
        [
            _basic_child(name="good", labels=["user"]),
            _basic_child(name="bad_labels", labels=["admin"]),
            _basic_child(name="bad_output", outputs=["_sys_key"]),
            _basic_child(name="good2", tool_scope=["read"]),
        ]
    )
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 2
    accepted_names = {c.name for c in accepted}
    assert accepted_names == {"good", "good2"}

    assert len(rejected) == 2
    rejected_names = {r["child_name"] for r in rejected}
    assert rejected_names == {"bad_labels", "bad_output"}


# ---------------------------------------------------------------------------
# v0.3.2 gaps: input containment
# ---------------------------------------------------------------------------


def test_child_input_not_in_parent_disclosure_rejected():
    """Child requests input parent can't see → rejected as input_not_authorized."""
    parent = _parent()
    # Parent disclosure only includes "visible_input"
    allowed = {"visible_input"}
    asset = _plan_asset([_basic_child(inputs=["hidden_input"])])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=allowed,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert rejected[0]["field"] == "inputs"
    assert rejected[0]["action"] == "rejected"
    assert "hidden_input" in rejected[0]["actual"]


def test_child_input_in_parent_disclosure_accepted():
    """Child input within parent disclosure scope → accepted."""
    parent = _parent()
    allowed = {"visible_input"}
    asset = _plan_asset(
        [_basic_child(inputs=["visible_input"], activation="visible_input")]
    )
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=allowed,
    )

    assert len(accepted) == 1
    assert accepted[0].inputs == ("visible_input",)
    # No input-related rejections
    input_rejections = [r for r in rejected if r["field"] == "inputs"]
    assert len(input_rejections) == 0


def test_child_input_is_own_output_accepted():
    """Child input that is also a child output (self-referential) → accepted."""
    parent = _parent()
    allowed: set[str] = set()
    asset = _plan_asset(
        [
            _basic_child(
                inputs=["draft_report"],
                outputs=["draft_report"],
                activation="draft_report",
            ),
        ]
    )
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=allowed,
    )

    assert len(accepted) == 1
    # No input-related rejections
    input_rejections = [r for r in rejected if r["field"] == "inputs"]
    assert len(input_rejections) == 0


# ---------------------------------------------------------------------------
# v0.3.2 gaps: fail-closed (parent not in store)
# ---------------------------------------------------------------------------


def test_missing_parent_fails_closed():
    """Parent not in store → no expansion, trace shows containment_rejected."""
    from aigineering.core.store import MemoryStore
    from aigineering.core.engine import Engine
    from aigineering.core.trace import TraceStore
    from aigineering.agent.mock import MockWorker

    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker()

    parent_id = "parent_not_in_store"

    # Create a method contract with parent_id pointing to non-existent parent.
    # The description must encode {"method": "plan"} so _expand_plan_result
    # recognises it as a plan method.
    import json

    method_id = hash_contract(
        "root.plan",
        json.dumps(
            {
                "method": "plan",
                "parent_contract_id": "x",
                "parent_contract_name": "root",
                "payload": {},
            },
            sort_keys=True,
        ),
        [],
        ["_plan_result_some"],
        "_method_ctx_some",
        1,
        [],
        [],
        "system",
    )
    from aigineering.protocol.types import Contract

    method_contract = Contract(
        id=method_id,
        parent_id=parent_id,
        name="root.plan",
        description=json.dumps(
            {
                "method": "plan",
                "parent_contract_id": "x",
                "parent_contract_name": "root",
                "payload": {},
            },
            sort_keys=True,
        ),
        origin="system",
        outputs=["_plan_result_some"],
        activation="_method_ctx_some",
        budget=1,
    )
    store.add_contract(method_contract)

    plan_asset = _plan_asset(
        [
            {
                "name": "draft",
                "description": "Draft.",
                "inputs": ["source"],
                "outputs": ["draft_report"],
                "activation": "source",
                "budget": 2,
            }
        ]
    )

    engine = Engine(store, worker, trace_store)
    engine._expand_plan_result(method_contract, [plan_asset])

    # Verify no child contracts were created
    planned = [
        c
        for c in store.get_all_contracts()
        if c.parent_id == parent_id and c.name == "draft"
    ]
    assert len(planned) == 0

    # Verify trace records the containment rejection
    containment = trace_store.get_by_event_type("containment_rejected")
    assert len(containment) == 1
    entry = containment[0]
    assert entry.relation_target == "parent_not_found"
    assert entry.authority_result == "rejected"
    assert "not in store" in entry.rejected_fragments[0]


# ---------------------------------------------------------------------------
# v0.3.2 gaps: budget fan-out across children
# ---------------------------------------------------------------------------


def test_budget_fanout_bounded_across_children():
    """3 children each budget=5, parent remaining=5 → later children clamped."""
    parent = _parent(budget=5)
    allowed = {"source"}
    children = [
        _basic_child(name="a", budget=5),
        _basic_child(name="b", budget=5),
        _basic_child(name="c", budget=5),
    ]
    asset = _plan_asset(children)
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=allowed,
        parent_budget_remaining=5,
    )

    assert len(accepted) == 3
    # First child gets full budget (5 ≤ 5), later children are clamped.
    assert accepted[0].budget == 5
    assert accepted[1].budget <= 2
    assert accepted[2].budget <= 2

    # At least 2 children have clamped budgets
    budget_clamps = [r for r in rejected if r["field"] == "budget"]
    assert len(budget_clamps) >= 2


def test_budget_fanout_not_exceeded():
    """Sum of child budgets does not exceed parent remaining."""
    parent = _parent(budget=10)
    allowed = {"source"}
    children = [
        _basic_child(name="a", budget=3),
        _basic_child(name="b", budget=3),
        _basic_child(name="c", budget=3),
    ]
    asset = _plan_asset(children)
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=allowed,
        parent_budget_remaining=10,
    )

    assert len(accepted) == 3
    total_budget = sum(c.budget for c in accepted)
    assert total_budget <= 10


# ---------------------------------------------------------------------------
# v0.3.2 gaps: reserved prefix consistency
# ---------------------------------------------------------------------------


def test_reserved_prefixes_include_all_authority_prefixes():
    """Plan containment also rejects outputs using authority.RESERVED_PREFIXES."""
    from aigineering.core.authority import RESERVED_PREFIXES

    parent = _parent()
    # Pick a prefix from authority.RESERVED_PREFIXES that wasn't in the old set
    # e.g. _tool_obs_, _tool_call_, _plan_result_, _replan_result_
    for prefix in [
        "_tool_obs_",
        "_tool_call_",
        "_plan_result_",
        "_replan_result_",
        "_fail_result_",
        "_method_ctx_",
        "_replan_report_",
        "_retry_",
    ]:
        assert prefix in RESERVED_PREFIXES, f"{prefix} should be in RESERVED_PREFIXES"

    # Test that _tool_obs_ output is rejected
    asset = _plan_asset([_basic_child(outputs=["_tool_obs_exfiltrate"])])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    assert rejected[0]["field"] == "outputs"
    assert rejected[0]["action"] == "rejected"
    assert "_tool_obs_exfiltrate" in rejected[0]["actual"]


def test_activation_containment_notes_unknown_refs():
    """Activation refs outside allowed scope are noted (not rejected)."""
    parent = _parent()
    allowed = {"visible_input"}
    # Input "visible_input" is in the allowed set to pass input containment.
    asset = _plan_asset(
        [_basic_child(inputs=["visible_input"], activation="sibling_output")]
    )
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=allowed,
    )

    # Child is still accepted (activation containment is "noted", not "rejected")
    assert len(accepted) == 1
    # The rejection entry should have action="noted" for benign scheduling refs
    activation_notes = [r for r in rejected if r["field"] == "activation"]
    assert len(activation_notes) >= 1
    assert activation_notes[0]["action"] == "noted"
    assert "sibling_output" in activation_notes[0]["actual"]
