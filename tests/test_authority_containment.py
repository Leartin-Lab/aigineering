"""Planner capability containment tests (v0.3.2)."""

import json

import pytest

from aigineering.core.ids import hash_contract, hash_contract_v3
from aigineering.core.methods import contracts_from_plan_asset
from aigineering.protocol.types import Asset, Contract


def _plan_asset(contracts: list[dict]) -> Asset:
    payload = json.dumps({"contracts": contracts}, sort_keys=True)
    return Asset(
        id="asset_plan",
        name="_plan_result_parent",
        content=payload,
    )


def _scaffold_asset(payload: dict) -> Asset:
    return Asset(
        id="asset_plan",
        name="_plan_result_parent",
        content=json.dumps(payload, sort_keys=True),
    )


def _raw_plan_asset(content: str) -> Asset:
    return Asset(
        id="asset_plan",
        name="_plan_result_parent",
        content=content,
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
# Labels select context assets, not business authority
# ---------------------------------------------------------------------------


def test_child_label_may_select_context_without_business_authority_check():
    """Planner labels are preserved for asset injection, not used as authority."""
    parent = _parent(labels=["user"])
    asset = _plan_asset([_basic_child(labels=["admin"])])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 1
    assert len(rejected) == 0
    assert accepted[0].labels == ("admin",)


def test_child_label_superset_is_preserved_for_context_injection():
    """Label subsets are not a business containment rule."""
    parent = _parent(labels=["user"])
    asset = _plan_asset([_basic_child(labels=["user", "admin"])])
    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
    )

    assert len(accepted) == 1
    assert len(rejected) == 0
    assert accepted[0].labels == ("user", "admin")


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
    """Planner-provided minting authority is ignored.

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


def test_planner_rejects_child_without_declared_outputs():
    """An ordinary planned task must have a fact it can commit."""
    parent = _parent(outputs=["final_report"])
    asset = _plan_asset([_basic_child(inputs=[], outputs=[])])

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=set(),
    )

    assert accepted == []
    assert any(
        item["child_name"] == "draft" and item["field"] == "outputs"
        for item in rejected
    )


def test_planner_rejects_child_without_executable_description():
    parent = _parent(outputs=["final_report"])
    asset = _plan_asset(
        [_basic_child(description="", inputs=[], outputs=["final_report"])]
    )

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=set(),
    )

    assert accepted == []
    assert any(item["field"] == "description" for item in rejected)


def test_legacy_plan_must_recommit_every_parent_output():
    """The legacy contracts schema has the same output coverage as scaffolds."""
    parent = _parent(outputs=["final_report"])
    asset = _plan_asset(
        [_basic_child(inputs=[], outputs=["intermediate"], activation="")]
    )

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=set(),
    )

    assert len(accepted) == 1
    assert any(item["field"] == "output_recommitment" for item in rejected)


def test_source_child_may_have_no_inputs_when_outputs_are_declared():
    """Source tasks remain valid; only the output side is mandatory."""
    parent = _parent(outputs=["final_report"])
    asset = _plan_asset(
        [_basic_child(inputs=[], outputs=["final_report"], activation="")]
    )

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=set(),
    )

    assert len(accepted) == 1
    assert accepted[0].inputs == ()
    assert rejected == []


def test_rejected_producer_cannot_leave_an_accepted_unreachable_consumer():
    parent = _parent(outputs=["final_report"])
    asset = _plan_asset(
        [
            _basic_child(
                name="invalid_source",
                description="",
                inputs=[],
                outputs=["intermediate"],
                activation="",
            ),
            _basic_child(
                name="consumer",
                inputs=["intermediate"],
                outputs=["final_report"],
                activation="intermediate",
            ),
        ]
    )

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=set(),
    )

    assert accepted == []
    assert any(item["child_name"] == "invalid_source" for item in rejected)
    assert any(
        item["child_name"] == "consumer" and item["field"] == "dependencies"
        for item in rejected
    )


def test_plan_dependency_cycle_without_a_grounded_source_is_rejected():
    parent = _parent(outputs=["final_report"])
    asset = _plan_asset(
        [
            _basic_child(
                name="left",
                inputs=["right_output"],
                outputs=["left_output"],
                activation="right_output",
            ),
            _basic_child(
                name="right",
                inputs=["left_output"],
                outputs=["right_output", "final_report"],
                activation="left_output",
            ),
        ]
    )

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=set(),
    )

    assert accepted == []
    assert {item["child_name"] for item in rejected} >= {"left", "right"}


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
    expected_id = hash_contract_v3(
        name="draft",
        description="Draft the report.",
        inputs=["source"],
        outputs=["draft_report"],
        activation="source",
        budget=2,
        tool_scope=["read"],
        labels=["user"],
        origin="plan",
        parent_id=parent.id,
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

    assert len(accepted) == 3
    accepted_names = {c.name for c in accepted}
    assert accepted_names == {"good", "bad_labels", "good2"}

    assert len(rejected) == 1
    rejected_names = {r["child_name"] for r in rejected}
    assert rejected_names == {"bad_output"}


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


def test_child_input_is_own_output_is_not_reachable():
    """A task cannot bootstrap the fact required to activate itself."""
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

    assert accepted == []
    assert any(item["field"] == "dependencies" for item in rejected)


def test_child_input_from_accepted_sibling_output_is_accepted():
    """A plan batch may wire one accepted child output into another child."""
    parent = _parent()
    allowed = {"source"}
    asset = _plan_asset(
        [
            _basic_child(
                name="gather",
                inputs=["source"],
                outputs=["notes"],
                activation="source",
            ),
            _basic_child(
                name="draft",
                inputs=["notes"],
                outputs=["draft_report"],
                activation="notes",
            ),
        ]
    )

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=allowed,
    )

    assert [c.name for c in accepted] == ["gather", "draft"]
    assert not [r for r in rejected if r.get("field") == "inputs"]
    assert not [r for r in rejected if r.get("field") == "activation"]


def test_rejected_sibling_output_does_not_authorize_consumer_input():
    """Only independently accepted siblings contribute future-output promises."""
    parent = _parent(tool_scope=["read"])
    allowed = {"source"}
    asset = _plan_asset(
        [
            _basic_child(
                name="bad_gather",
                inputs=["source"],
                outputs=["notes"],
                activation="source",
                tool_scope=["write"],
            ),
            _basic_child(
                name="draft",
                inputs=["notes"],
                outputs=["draft_report"],
                activation="notes",
                labels=["user"],
            ),
        ]
    )

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=allowed,
    )

    assert accepted == []
    rejected_fields = {(r["child_name"], r["field"]) for r in rejected}
    assert ("bad_gather", "tool_scope") in rejected_fields
    assert ("draft", "inputs") in rejected_fields


def test_sibling_output_from_unreachable_input_producer_does_not_authorize_consumer():
    """A producer rejected for hidden inputs must not contribute output promises."""
    parent = _parent()
    allowed = {"source"}
    asset = _plan_asset(
        [
            _basic_child(
                name="bad_gather",
                inputs=["hidden_source"],
                outputs=["notes"],
                activation="hidden_source",
            ),
            _basic_child(
                name="draft",
                inputs=["notes"],
                outputs=["draft_report"],
                activation="notes",
            ),
        ]
    )

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=allowed,
    )

    assert accepted == []
    rejected_fields = {(r["child_name"], r["field"]) for r in rejected}
    assert ("bad_gather", "inputs") in rejected_fields
    assert ("draft", "inputs") in rejected_fields


def test_sibling_output_promise_is_order_independent():
    """A consumer may appear before its producer in the same accepted batch."""
    parent = _parent()
    allowed = {"source"}
    asset = _plan_asset(
        [
            _basic_child(
                name="draft",
                inputs=["notes"],
                outputs=["draft_report"],
                activation="notes",
            ),
            _basic_child(
                name="gather",
                inputs=["source"],
                outputs=["notes"],
                activation="source",
            ),
        ]
    )

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names=allowed,
    )

    assert {c.name for c in accepted} == {"gather", "draft"}
    assert not [r for r in rejected if r.get("field") in {"inputs", "activation"}]


def test_scaffold_placeholder_names_compile_before_containment():
    """Structured plans may use symbolic names that compile before validation."""
    parent = _parent(inputs=["source"], outputs=["final_report"])
    asset = _scaffold_asset(
        {
            "reason": "need intermediate evidence",
            "goal_outline": "produce final report",
            "intermediate_assets": ["{notes}"],
            "step_1_tasks": [
                {
                    "name": "gather",
                    "description": "Gather notes.",
                    "budget": 1,
                    "tool_scope": ["read"],
                    "labels": ["user"],
                },
                {
                    "name": "draft",
                    "description": "Draft final report.",
                    "budget": 1,
                    "tool_scope": ["read"],
                    "labels": ["user"],
                },
            ],
            "step_2_data_flow": [
                {
                    "task_name": "gather",
                    "consumes": ["source"],
                    "produces": ["{notes}"],
                },
                {
                    "task_name": "draft",
                    "consumes": ["{notes}"],
                    "produces": ["final_report"],
                },
            ],
            "step_3_activation": [
                {
                    "task_name": "gather",
                    "expression": "source",
                    "depends_on": ["source"],
                },
                {
                    "task_name": "draft",
                    "expression": "{notes}",
                    "depends_on": ["{notes}"],
                },
            ],
        }
    )

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names={"source"},
    )

    assert {c.name for c in accepted} == {"gather", "draft"}
    draft = next(c for c in accepted if c.name == "draft")
    assert draft.inputs == ("notes",)
    assert draft.activation == "notes"
    assert not [r for r in rejected if r.get("field") in {"inputs", "activation"}]


def test_unsupported_plan_result_schema_is_recoverable_rejection():
    parent = _parent()
    asset = _raw_plan_asset(
        json.dumps(
            {
                "plan_name": "bad_shape",
                "child_contracts": [
                    {
                        "contract_name": "draft",
                        "expected_outputs": ["draft_report"],
                    }
                ],
            },
            sort_keys=True,
        )
    )

    accepted, rejected = contracts_from_plan_asset(
        asset,
        parent.id,
        parent_contract=parent,
        allowed_input_names={"source"},
    )

    assert accepted == []
    assert len(rejected) == 1
    assert rejected[0]["field"] == "schema"
    assert rejected[0]["recoverable"] is True


# ---------------------------------------------------------------------------
# v0.3.2 gaps: fail-closed (parent not in store)
# ---------------------------------------------------------------------------


def test_budget_fanout_bounded_across_children():
    """Children are rejected once no positive parent allowance remains."""
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

    assert len(accepted) == 1
    assert accepted[0].budget == 5
    assert sum(child.budget for child in accepted) <= 5
    exhausted = [
        finding
        for finding in rejected
        if finding["field"] == "budget" and finding["action"] == "rejected"
    ]
    assert len(exhausted) == 2
    assert all(finding["effective"] == 0 for finding in exhausted)


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


def test_activation_containment_rejects_refs_with_no_accepted_producer():
    """An unknown activation name must not leave a permanently blocked task."""
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

    assert accepted == []
    activation_notes = [r for r in rejected if r["field"] == "activation"]
    assert len(activation_notes) >= 1
    assert activation_notes[0]["action"] == "noted"
    assert "sibling_output" in activation_notes[0]["actual"]
    assert any(r["field"] == "dependencies" for r in rejected)
