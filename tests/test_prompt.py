"""Tests for worker prompt construction."""

from aigineering.agent.prompt import contract_prompt, system_prompt
from aigineering.protocol.types import Asset, Contract


def test_system_prompt_preserves_candidate_boundary():
    prompt = system_prompt()

    assert "candidate" in prompt
    assert "not committed state" in prompt
    assert "/exec" in prompt
    assert "/plan" in prompt
    assert "/replan" in prompt
    assert "/tool" in prompt
    assert "/retry" in prompt
    assert "/fail" in prompt
    assert "declared output names" in prompt


def test_system_prompt_preserves_plan_replan_boundary():
    prompt = system_prompt()

    assert "needs more information" in prompt
    assert "use `/plan" in prompt
    assert "gone off course" in prompt
    assert "use `/replan" in prompt


def test_system_prompt_distinguishes_retry_and_fail():
    prompt = system_prompt()

    assert "transient execution or output failure" in prompt
    assert "use `/retry" in prompt
    assert "no safe plan or allowed tool can obtain it" in prompt
    assert "fabricated facts" in prompt
    assert "use `/fail" in prompt


def test_contract_prompt_renders_declared_scope():
    contract = Contract(
        id="contract_1",
        name="write_report",
        description="Write a report.",
        inputs=["evidence"],
        outputs=["report"],
        tool_scope=["search"],
    )
    prompt = contract_prompt(
        contract,
        [Asset(id="asset_1", name="evidence", content="observed")],
    )

    assert "Contract name: write_report" in prompt
    assert "Declared inputs: evidence" in prompt
    assert "Declared outputs: report" in prompt
    assert "Allowed tools: search" in prompt
    assert f"Available causal allowance: {contract.budget}" in prompt
    assert "exact readable content" in prompt
    assert "not as a path or handle" in prompt
    assert '/exec {"outputs": {"declared_output": "content"}}' in prompt
    assert "- evidence [asset_1]: observed" in prompt


def test_contract_prompt_preserves_plan_replan_boundary():
    contract = Contract(id="contract_1", name="write_report")
    prompt = contract_prompt(contract, [])

    assert "current task needs more information" in prompt
    assert "current task has already gone off course" in prompt
    assert "Do not use /replan for missing information" in prompt


def test_contract_prompt_renders_retry_and_fail_decision_boundary():
    contract = Contract(id="contract_1", name="write_report")
    prompt = contract_prompt(contract, [])

    assert '- /retry {"reason":' in prompt
    assert '- /fail {"reason":' in prompt
    assert "same task can be attempted again" in prompt
    assert "new evidence or a different plan" in prompt
    assert "completion would require fabricated facts" in prompt


def test_verifier_prompt_distinguishes_target_from_current_contract():
    prompt = contract_prompt(
        Contract(
            id="task:verifier",
            description="Attest exact target task:root",
            outputs=("verification_receipt",),
        ),
        [],
    )

    assert "not the current verifier Contract ID" in prompt
    assert "copy that ID exactly" in prompt


def test_contract_prompt_does_not_teach_single_call_parallel_tool():
    prompt = contract_prompt(
        Contract(id="tool-task", tool_scope=("lookup", "metadata")), []
    )

    assert "2-8 independent calls only" in prompt
    assert "use /tool for one call" in prompt
    assert '"name": "tool_a"' in prompt
    assert '"name": "tool_b"' in prompt


def test_planned_tool_task_requires_dispatch_before_exec_output():
    prompt = contract_prompt(
        Contract(
            id="retrieve",
            origin="plan",
            outputs=("manifest",),
            tool_scope=("openalex_search",),
        ),
        [],
    )

    assert "REQUIRED NEXT ACTION" in prompt
    assert "do not return /exec yet" in prompt
    assert "openalex_search" in prompt


def test_human_root_with_delegable_tool_scope_can_still_plan():
    prompt = contract_prompt(
        Contract(id="root", origin="human", tool_scope=("openalex_search",)), []
    )

    assert "REQUIRED NEXT ACTION" not in prompt
    assert '/plan {"reason":' in prompt


def test_prompt_renders_immutable_deterministic_output_shape():
    prompt = contract_prompt(
        Contract(
            id="shaped",
            outputs=("report",),
            acceptance_policy={
                "mode": "mechanical",
                "output_shapes": {
                    "report": {"answer": "nonempty_string", "citations": ["string"]}
                },
            },
        ),
        [],
    )

    assert "Deterministic output shape" in prompt
    assert '"citations": ["string"]' in prompt


def test_contract_prompt_separates_behavior_instructions_from_assets():
    contract = Contract(
        id="contract_1",
        name="write_report",
        description="Write a report.",
        inputs=["evidence"],
        outputs=["report"],
    )
    prompt = contract_prompt(
        contract,
        [
            Asset(id="asset_1", name="evidence", content="observed"),
            Asset(id="asset_2", name="behavior:concise", content="be concise"),
        ],
    )

    behavior_section = prompt.split("Behavior instructions:", 1)[1].split(
        "Disclosed assets:", 1
    )[0]
    disclosed_section = prompt.split("Disclosed assets:", 1)[1]

    assert "- behavior:concise: be concise" in behavior_section
    assert "- evidence [asset_1]: observed" in disclosed_section
    assert "behavior:concise" not in disclosed_section


def test_planner_prompt_requires_boolean_activation_grammar():
    contract = Contract(
        id="plan-task",
        name="root.plan",
        outputs=("_plan_result_root",),
    )

    prompt = contract_prompt(contract, [])

    assert "input_a AND input_b" in prompt
    assert "never use commas" in prompt


def test_compile_prompt_exposes_schema_output_coverage_and_allowance():
    contract = Contract(
        id="compile-task",
        name="root.plan.compile",
        outputs=("final_report",),
        labels=("plugin:plan.compile",),
        budget=3,
        description='{"allowed_inputs":["source"]}',
        inputs=("source", "plan_draft_internal", "plan_dependencies_internal"),
    )

    prompt = contract_prompt(contract, [])

    assert "non-empty executable description" in prompt
    assert "Sum of child budgets must be at most 3" in prompt
    assert "child outputs must collectively include: final_report" in prompt
    assert '"outputs":["final_report"]' in prompt
    assert '"inputs":["source"]' in prompt
    assert "Never copy their names into child inputs" in prompt
    assert '["source"]' in prompt
    assert "labels list must be a subset of []" in prompt
    assert "never invent labels or copy plugin:* labels" in prompt


def test_compile_prompt_example_never_exceeds_one_remaining_allowance():
    contract = Contract(
        id="compile-task",
        name="root.plan.compile",
        outputs=("summary", "appendix"),
        labels=("plugin:plan.compile",),
        budget=1,
    )

    prompt = contract_prompt(contract, [])

    assert "Sum of child budgets must be at most 1" in prompt
    assert prompt.count('"budget":1') == 1
    assert '"outputs":["summary","appendix"]' in prompt


def test_draft_stage_prompt_requires_exec_with_exact_output_name():
    contract = Contract(
        id="draft-task",
        name="root.plan.draft",
        outputs=("plan_draft_123",),
        labels=("plugin:plan.draft",),
    )

    prompt = contract_prompt(contract, [])

    assert "exactly one output named `plan_draft_123`" in prompt
    assert "do not return a bare JSON object" in prompt


def test_fail_task_prompt_requires_terminal_exec_instead_of_recursive_fail():
    contract = Contract(
        id="fail-task",
        name="parent.fail",
        description='{"reason":"evidence unavailable"}',
        outputs=("failure_result",),
        labels=("plugin:fail",),
        origin="system",
    )

    prompt = contract_prompt(contract, [])

    assert "Failure reporting task protocol" in prompt
    assert "do not use /fail again" in prompt
    assert "Return /exec with exactly the declared output name" in prompt


def test_recovery_task_prompt_requires_exact_declared_outputs():
    contract = Contract(
        id="recovery-task",
        name="parent.recover",
        outputs=("report",),
        origin="recovery",
    )

    prompt = contract_prompt(contract, [])

    assert "Recovery task protocol" in prompt
    assert "exactly the declared output names" in prompt


def test_tool_recovery_may_reacquire_observation_without_inventing_evidence():
    contract = Contract(
        id="recovery-tool-task",
        name="retrieve.recover",
        outputs=("manifest",),
        tool_scope=("search",),
        origin="recovery",
    )

    prompt = contract_prompt(contract, [])

    assert "Use /tool for one call" in prompt
    assert "Never construct a tool-derived" in prompt
