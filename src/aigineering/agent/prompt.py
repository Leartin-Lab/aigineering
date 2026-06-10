"""Prompt construction for candidate-producing workers."""

from __future__ import annotations

from aigineering.protocol.types import Asset, Contract


def system_prompt() -> str:
    """Return the invariant-preserving system prompt."""

    return (
        "You are an Aigineering worker. Your output is only a candidate, "
        "not committed state. Return only asset lines in the exact format "
        "`asset_name: content`. Use only declared output names. Do not add "
        "markdown, explanations, or undeclared assets."
    )


def contract_prompt(contract: Contract, assets: list[Asset]) -> str:
    """Render a contract and disclosed assets for a worker."""

    lines = [
        f"Contract name: {contract.name}",
        f"Description: {contract.description}",
        "Declared inputs: " + ", ".join(contract.inputs),
        "Declared outputs: " + ", ".join(contract.outputs),
        "",
        "Disclosed assets:",
    ]
    for asset in assets:
        lines.append(f"- {asset.name}: {asset.content}")
    return "\n".join(lines)
