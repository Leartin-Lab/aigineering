"""Tests for worker prompt construction."""

from aigineering.agent.prompt import contract_prompt, system_prompt
from aigineering.protocol.types import Asset, Contract


def test_system_prompt_preserves_candidate_boundary():
    prompt = system_prompt()

    assert "candidate" in prompt
    assert "not committed state" in prompt
    assert "declared output names" in prompt


def test_contract_prompt_renders_declared_scope():
    contract = Contract(
        id="contract_1",
        name="write_report",
        description="Write a report.",
        inputs=["evidence"],
        outputs=["report"],
    )
    prompt = contract_prompt(
        contract,
        [Asset(id="asset_1", name="evidence", content="observed")],
    )

    assert "Contract name: write_report" in prompt
    assert "Declared inputs: evidence" in prompt
    assert "Declared outputs: report" in prompt
    assert "- evidence: observed" in prompt
