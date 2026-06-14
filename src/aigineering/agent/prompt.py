"""Prompt construction for candidate-producing workers."""

from __future__ import annotations

from aigineering.protocol.types import Asset, Contract


def system_prompt() -> str:
    """Return the invariant-preserving system prompt."""

    return (
        "You are an Aigineering worker. Your output is only a candidate, "
        "not committed state. Return exactly one structured action. To "
        'produce final outputs, use `/exec {"outputs": {"asset_name": '
        '"content"}}`. Use only declared output names. If you need task '
        'decomposition, use `/plan {"reason": "..."}`. If you need '
        'recovery, use `/replan {"reason": "..."}`. If you need an '
        'allowed tool, use `/tool {"name": "...", "args": {}}`. Do '
        "not add markdown, explanations, or undeclared assets."
    )


def contract_prompt(contract: Contract, assets: list[Asset]) -> str:
    """Render a contract and disclosed assets for a worker."""

    lines = [
        f"Contract name: {contract.name}",
        f"Description: {contract.description}",
        "Declared inputs: " + ", ".join(contract.inputs),
        "Declared outputs: " + ", ".join(contract.outputs),
        "Allowed tools: " + ", ".join(contract.tool_scope),
        "",
        "Return format:",
        '- /exec {"outputs": {"declared_output": "content"}}',
        '- /plan {"reason": "why decomposition is required"}',
        '- /replan {"reason": "why recovery is required"}',
        '- /tool {"name": "tool_name", "args": {}}',
        "",
        "Disclosed assets:",
    ]
    for asset in assets:
        lines.append(f"- {asset.name}: {asset.content}")
    return "\n".join(lines)
