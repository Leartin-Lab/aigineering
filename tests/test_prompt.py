"""Tests for worker prompt construction."""

from aigineering.agent.prompt import contract_prompt, system_prompt
from aigineering.agent.worker import compile_worker_envelope
from aigineering.core.signing import Ed25519Signer
from aigineering.protocol.actions import parse_action
from aigineering.protocol.asset_graph import content_object_from_dict
from aigineering.protocol.candidate import ActorKey
from aigineering.protocol.envelope import CandidateEnvelope
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


def test_multiline_markdown_example_is_strict_json_and_compiles_exact_slot():
    contract = Contract(
        id="task:markdown",
        name="write_report",
        description="Write readable Markdown.",
        outputs=("markdown_report",),
    )
    prompt = contract_prompt(contract, [])
    assert "Escape line breaks as \\n" in prompt
    assert 'quotes as \\"' in prompt
    example = prompt.split(
        "Valid multiline Markdown example for this exact declared output:\n", 1
    )[1].splitlines()[0]
    action = parse_action(example)
    expected = '# Summary\n\nThe path is C:\\reports and the reviewer wrote "ready".'
    assert dict(action.outputs) == {"markdown_report": expected}

    signer = Ed25519Signer()
    actor_key = ActorKey(
        "worker:prompt-test",
        "prompt-test-1",
        signer.kind,
        signer.signer_id,
        ("worker.submit",),
    )
    envelope = CandidateEnvelope(
        contract_id=contract.id,
        worker_id=actor_key.actor_id,
        raw_output=example,
        package_id="pkg:prompt-test",
        claim_id="claim:prompt-test",
        claim_epoch=1,
        idempotency_key="prompt-test-output",
    )
    candidate = compile_worker_envelope(
        envelope,
        domain_id="domain:prompt-test",
        actor_key=actor_key,
        signer=signer,
        contract=contract,
    )
    content_effects = [
        effect
        for effect in candidate.effects
        if effect.effect_type == "asset.content.publish"
    ]
    assert len(content_effects) == 1
    value = content_object_from_dict(content_effects[0].payload["content"])
    assert value.content == expected


def test_prompt_omits_markdown_example_for_planning_control_stages():
    for label in (
        "plugin:plan.draft",
        "plugin:plan.dependencies",
        "plugin:plan.compile",
        "plugin:replan.draft",
        "plugin:replan.dependencies",
        "plugin:replan.compile",
    ):
        prompt = contract_prompt(
            Contract(
                id=f"{label}:test",
                description="Your first action must be /plan because the original goal required a child.",
                outputs=("stage_result",),
                labels=(label,),
            ),
            [],
        )
        assert "JSON-encode every output string" in prompt
        assert "Valid multiline Markdown example" not in prompt
        assert "already-issued /plan or /replan action" in prompt
        assert "Complete this stage with the declared /exec output" in prompt
        assert "does not prohibit a new /plan or /replan" in prompt


def test_ordinary_task_can_still_plan_recursively():
    prompt = contract_prompt(
        Contract(
            id="ordinary-task",
            description="If this work needs more evidence, request /plan.",
            outputs=("report",),
        ),
        [],
    )

    assert "Use /plan when disclosed information is insufficient." in prompt
    assert "already-issued /plan or /replan action" not in prompt


def test_prompt_omits_markdown_example_for_shaped_and_protected_outputs():
    shaped = contract_prompt(
        Contract(
            id="shaped-report",
            outputs=("report",),
            acceptance_policy={
                "mode": "mechanical",
                "output_shapes": {"report": {"answer": "nonempty_string"}},
            },
        ),
        [],
    )
    protected = contract_prompt(
        Contract(id="planner-result", outputs=("_plan_result_abc",)), []
    )

    assert "Valid multiline Markdown example" not in shaped
    assert "Valid multiline Markdown example" not in protected
    assert "JSON-encode every output string" in shaped


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


def test_plan_compile_preserves_explicit_direct_child_and_future_replan_scope():
    contract = Contract(
        id="compile-task",
        name="root.plan.compile",
        outputs=("final_report",),
        labels=("plugin:plan.compile",),
        budget=19,
        inputs=(
            "current_claim",
            "revision_notice",
            "plan_draft_internal",
            "plan_dependencies_internal",
        ),
        description='{"allowed_inputs":["current_claim","revision_notice"]}',
    )
    prompt = contract_prompt(
        contract,
        [
            Asset(
                id="draft-asset",
                name="plan_draft_internal",
                content=(
                    '{"proposed_steps":[{"name":"inspect_revision",'
                    '"inputs":["current_claim","revision_notice"],'
                    '"outputs":["final_report"],"budget":8}]}'
                ),
            ),
            Asset(
                id="dependency-asset",
                name="plan_dependencies_internal",
                content=(
                    '{"allowance_needs":{"inspect_revision":8,'
                    '"reconcile_revision":1},"activation":"both inputs"}'
                ),
            ),
        ],
    )

    assert "required direct child names and count" in prompt
    assert "inputs, outputs, activation, budgets, and allowance_needs" in prompt
    assert (
        "future replan successors belong under that child's later replan compiler"
        in prompt
    )
    assert "Multiple independent direct children remain valid" in prompt
    assert "at least the three allowance units for its planning stages" in prompt
    assert "JSON string in its declared output" in prompt
    assert "stage document object or array directly" in prompt
    assert '"budget":8' in prompt
    assert '"reconcile_revision":1' in prompt
    assert "required direct child names and count" in prompt


def test_replan_compile_keeps_direct_successor_compilation_available():
    contract = Contract(
        id="recompile-task",
        name="child.replan.compile",
        outputs=("final_report",),
        labels=("plugin:replan.compile",),
        budget=5,
    )
    prompt = contract_prompt(contract, [])

    assert "declares direct successors for the current replanning Contract" in prompt
    assert "Preserve the successor requirements and budgets" in prompt
    assert "future work under that successor's later replan compiler" in prompt
    assert "temporary output `planning_blueprint`" in prompt
    assert "temporary output `final_report`" not in prompt


def test_planning_stage_action_wire_instructions_cover_all_stage_types():
    labels = (
        "plugin:plan.draft",
        "plugin:plan.dependencies",
        "plugin:plan.compile",
        "plugin:replan.draft",
        "plugin:replan.dependencies",
        "plugin:replan.compile",
    )
    for label in labels:
        contract = Contract(
            id=label,
            name="stage",
            outputs=("stage_output",),
            labels=(label,),
        )
        prompt = contract_prompt(contract, [])
        assert "JSON string in its declared output" in prompt
        assert "place a stage document object or array directly" in prompt
        assert "never return the stage document as a bare top-level object" in prompt
        if label in {"plugin:plan.compile", "plugin:replan.compile"}:
            assert "temporary output `planning_blueprint`" in prompt
            assert "temporary output `stage_output`" not in prompt
        else:
            assert "output `stage_output`" in prompt


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
    assert "never return the stage document as a bare top-level object" in prompt


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
