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
    assert "declared output names" in prompt


def test_system_prompt_preserves_plan_replan_boundary():
    prompt = system_prompt()

    assert "needs more information" in prompt
    assert "use `/plan" in prompt
    assert "gone off course" in prompt
    assert "use `/replan" in prompt


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
    assert '/exec {"outputs": {"declared_output": "content"}}' in prompt
    assert "- evidence: observed" in prompt


def test_contract_prompt_preserves_plan_replan_boundary():
    contract = Contract(id="contract_1", name="write_report")
    prompt = contract_prompt(contract, [])

    assert "current task needs more information" in prompt
    assert "current task has already gone off course" in prompt
    assert "Do not use /replan for missing information" in prompt


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
    assert "- evidence: observed" in disclosed_section
    assert "behavior:concise" not in disclosed_section
