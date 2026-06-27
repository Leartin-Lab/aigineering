"""Prompt construction for candidate-producing workers."""

from __future__ import annotations

from aigineering.core.labels import BEHAVIOR_LABEL_PREFIX
from aigineering.protocol.types import Asset, Contract


def system_prompt() -> str:
    """Return the invariant-preserving system prompt."""

    return (
        "You are an Aigineering worker. Your output is only a candidate, "
        "not committed state. Return exactly one structured action. To "
        'produce final outputs, use `/exec {"outputs": {"asset_name": '
        '"content"}}`. Use only declared output names. If you need task '
        "decomposition because the current task needs more information, use "
        '`/plan {"reason": "..."}`. If the current task has already gone '
        "off course because an assumption, path, or result is invalid, use "
        '`/replan {"reason": "..."}`. If you need an allowed tool, use '
        '`/tool {"name": "...", "args": {}}`. Do not use /replan for '
        "missing information. Do not add markdown, explanations, or "
        "undeclared assets."
    )


def contract_prompt(contract: Contract, assets: list[Asset]) -> str:
    """Render a contract and disclosed assets for a worker."""

    behavior_assets = [
        asset for asset in assets if asset.name.startswith(BEHAVIOR_LABEL_PREFIX)
    ]
    evidence_assets = [
        asset for asset in assets if not asset.name.startswith(BEHAVIOR_LABEL_PREFIX)
    ]

    lines = [
        f"Contract name: {contract.name}",
        f"Description: {contract.description}",
        "Declared inputs: " + ", ".join(contract.inputs),
        "Declared outputs: " + ", ".join(contract.outputs),
        "Allowed tools: " + ", ".join(contract.tool_scope),
        "",
        "Return format:",
        '- /exec {"outputs": {"declared_output": "content"}}',
        '- /plan {"reason": "why the current task needs more information"}',
        '- /replan {"reason": "why the current task has already gone off course"}',
        '- /tool {"name": "tool_name", "args": {}}',
        "",
        "Decision boundary:",
        "- Use /plan when disclosed information is insufficient.",
        "- Use /replan only after an assumption, path, or result is invalid.",
        "- Do not use /replan for missing information.",
        "",
        "Behavior instructions:",
    ]
    for asset in behavior_assets:
        lines.append(f"- {asset.name}: {asset.content}")

    lines.extend(
        [
            "",
            "Disclosed assets:",
        ]
    )
    for asset in evidence_assets:
        lines.append(f"- {asset.name}: {asset.content}")
    return "\n".join(lines)
