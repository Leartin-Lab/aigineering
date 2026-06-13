"""Candidate projection — the commitment boundary where raw output becomes facts."""

from __future__ import annotations

import json
from collections.abc import Mapping

from aigineering.core.authority import check_authority
from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.protocol.actions import (
    ActionParseError,
    WorkerAction,
    action_from_dict,
    parse_action,
)
from aigineering.protocol.types import (
    Asset,
    Candidate,
    Contract,
    ProjectionResult,
    ProjectionStatus,
    RejectedCandidate,
    RejectionCategory,
)


def project_candidate(
    contract: Contract,
    candidate: Candidate,
) -> ProjectionResult:
    fragments, parse_rejected = _parse_candidate_fragments(candidate)
    if not fragments and not parse_rejected:
        parse_rejected.append(
            RejectedCandidate(
                name="(empty)",
                content="",
                reject_reason="worker produced no candidate fragments",
                category=RejectionCategory.PARSE_ERROR,
            )
        )

    # Reject duplicate names with conflicting content: reject ALL for that name
    seen_names: dict[str, str] = {}  # name → first content
    conflict_names: set[str] = set()
    for f in fragments:
        name = f["name"]
        if name in seen_names:
            if seen_names[name] != f["content"]:
                conflict_names.add(name)
        else:
            seen_names[name] = f["content"]

    rejected_name_set: set[str] = {r.name for r in parse_rejected}
    deduped_fragments: list[dict] = []
    for f in fragments:
        name = f["name"]
        if name in conflict_names:
            if name not in rejected_name_set:
                parse_rejected.append(
                    RejectedCandidate(
                        name=name,
                        content=f["content"],
                        reject_reason=f"duplicate output '{name}' with conflicting content — all instances rejected",
                        category=RejectionCategory.DUPLICATE_REJECTION,
                    )
                )
                rejected_name_set.add(name)
        else:
            deduped_fragments.append(f)

    accepted_dicts, authority_rejected_dicts, authority_policy = check_authority(contract, deduped_fragments)
    accepted_assets: list[Asset] = []

    for a in accepted_dicts:
        worker_origin = _derive_worker_origin(candidate.worker_id)
        asset = Asset(
            id=hash_asset_content(a["name"], a["content"]),
            name=a["name"],
            content=a["content"],
            definition_hash=hash_asset_definition(a["name"]),
            content_hash=hash_asset_content(a["name"], a["content"]),
            content_type="text",
            created_by=contract.id,
            origin=worker_origin,
            trust_tier="untrusted",
            minted_by=candidate.worker_id,
            source_uri="",
            promptable=True,
            disclosure_view="original",
        )
        accepted_assets.append(asset)

    _cat_map = {
        "authority_rejection": RejectionCategory.AUTHORITY_REJECTION,
        "protected_name_rejection": RejectionCategory.PROTECTED_NAME_REJECTION,
    }
    all_rejected: list[RejectedCandidate] = list(parse_rejected)
    for r in authority_rejected_dicts:
        raw_cat = r.get("category", "authority_rejection")
        all_rejected.append(
            RejectedCandidate(
                name=r["name"],
                content=r["content"],
                reject_reason=r["reject_reason"],
                category=_cat_map.get(raw_cat, RejectionCategory.AUTHORITY_REJECTION),
            )
        )

    # Determine projection status
    if not all_rejected:
        status = ProjectionStatus.ACCEPTED
    elif not accepted_assets:
        status = ProjectionStatus.REJECTED
    else:
        status = ProjectionStatus.PARTIAL

    return ProjectionResult(
        accepted_assets=accepted_assets,
        rejected_candidates=all_rejected,
        raw_candidate=candidate.raw_output,
        status=status,
        authority_policy=authority_policy,
    )


def _parse_candidate_fragments(
    candidate: Candidate,
) -> tuple[list[dict], list[RejectedCandidate]]:
    parsed = candidate.parsed_action
    if isinstance(parsed, Mapping) and isinstance(parsed.get("type"), str):
        try:
            return _fragments_from_action(action_from_dict(parsed))
        except ActionParseError as e:
            return [], [
                RejectedCandidate(
                    name="(action)",
                    content=str(parsed)[:120],
                    reject_reason=str(e),
                    category=RejectionCategory.PARSE_ERROR,
                )
            ]

    if candidate.raw_output.strip().startswith("/"):
        try:
            return _fragments_from_action(parse_action(candidate.raw_output))
        except (ActionParseError, json.JSONDecodeError) as e:
            return [], [
                RejectedCandidate(
                    name="(action)",
                    content=candidate.raw_output[:120],
                    reject_reason=str(e),
                    category=RejectionCategory.PARSE_ERROR,
                )
            ]

    return _parse_legacy_lines(candidate.raw_output)


def _fragments_from_action(
    action: WorkerAction,
) -> tuple[list[dict], list[RejectedCandidate]]:
    if action.type != "exec":
        return [], [
            RejectedCandidate(
                name=f"/{action.type}",
                content="",
                reject_reason=f"/{action.type} cannot produce committed assets through projection",
                category=RejectionCategory.PARSE_ERROR,
            )
        ]
    return [
        {"name": name, "content": content}
        for name, content in action.outputs.items()
    ], []


def _parse_legacy_lines(raw_output: str) -> tuple[list[dict], list[RejectedCandidate]]:
    fragments: list[dict] = []
    parse_rejected: list[RejectedCandidate] = []

    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            parse_rejected.append(
                RejectedCandidate(
                    name=stripped[:40],
                    content="",
                    reject_reason=f"unparsable line (no colon separator): '{stripped[:80]}'",
                    category=RejectionCategory.PARSE_ERROR,
                )
            )
            continue
        name, _, content = stripped.partition(":")
        name = name.strip()
        content = content.strip()
        if not name:
            parse_rejected.append(
                RejectedCandidate(
                    name="(empty)",
                    content=content,
                    reject_reason=f"empty asset name in line: '{stripped[:80]}'",
                    category=RejectionCategory.PARSE_ERROR,
                )
            )
            continue
        if not content:
            parse_rejected.append(
                RejectedCandidate(
                    name=name,
                    content="",
                    reject_reason=f"empty content for asset '{name}'",
                    category=RejectionCategory.PARSE_ERROR,
                )
            )
            continue
        fragments.append({"name": name, "content": content})

    return fragments, parse_rejected


def _derive_worker_origin(worker_id: str) -> str:
    """Derive the asset ``origin`` from the worker's canonical type prefix.

    Maps known worker_id prefixes to their corresponding origin category:
    ``llm:`` → ``"llm"``, ``tool_worker:`` → ``"tool"``,
    ``mcp_worker:`` → ``"mcp"``, ``mock`` → ``"mock"``.
    Falls back to ``"worker"`` for unknown workers.
    """
    if worker_id.startswith("llm:"):
        return "llm"
    if worker_id.startswith("tool_worker:"):
        return "tool"
    if worker_id.startswith("mcp_worker:"):
        return "mcp"
    if worker_id == "mock_worker":
        return "mock"
    return "worker"
