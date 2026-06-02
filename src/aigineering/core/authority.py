"""Authority checker — the commitment boundary."""

from __future__ import annotations

from aigineering.protocol.types import Contract

RESERVED_PREFIXES: frozenset[str] = frozenset(
    {
        "_sys_",
        "_skill_",
        "_memory_",
        "_soul_",
        "_mcp_",
        "_tool_obs_",
        "_tool_call_",
        "_plan_result_",
        "_replan_result_",
        "_fail_result_",
        "_method_ctx_",
        "_replan_report_",
        "_fail_report_",
        "_retry_",
    }
)


def check_authority(
    contract: Contract,
    candidate_assets: list[dict],
) -> tuple[list[dict], list[dict]]:
    accepted: list[dict] = []
    rejected: list[dict] = []

    for candidate in candidate_assets:
        name: str = candidate["name"]
        reasons: list[str] = []

        if name not in contract.outputs:
            reasons.append(
                f"asset '{name}' is not in contract.outputs "
                f"({contract.outputs!r})"
            )

        for prefix in RESERVED_PREFIXES:
            if name.startswith(prefix):
                reasons.append(
                    f"asset '{name}' starts with reserved prefix '{prefix}'"
                )
                break

        if reasons:
            rejected.append(
                {
                    "name": name,
                    "content": candidate["content"],
                    "reject_reason": "; ".join(reasons),
                }
            )
        else:
            accepted.append({"name": name, "content": candidate["content"]})

    return accepted, rejected
