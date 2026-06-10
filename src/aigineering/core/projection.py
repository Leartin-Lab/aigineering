"""Candidate projection — the commitment boundary where raw output becomes facts."""

from __future__ import annotations

import json

from aigineering.core.authority import check_authority
from aigineering.core.ids import asset_id
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
    fragments: list[dict] = []
    parse_rejected: list[RejectedCandidate] = []

    for line in candidate.raw_output.splitlines():
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
        canonical_str = json.dumps(
            {
                "name": a["name"],
                "content": a["content"],
                "content_type": "text",
                "created_by": contract.id,
                "origin": "mock",
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        asset = Asset(
            id=asset_id(canonical_str),
            name=a["name"],
            content=a["content"],
            content_type="text",
            created_by=contract.id,
            origin="mock",
        )
        accepted_assets.append(asset)

    # Convert authority-rejected dicts to RejectedCandidate objects
    all_rejected: list[RejectedCandidate] = list(parse_rejected)
    for r in authority_rejected_dicts:
        all_rejected.append(
            RejectedCandidate(
                name=r["name"],
                content=r["content"],
                reject_reason=r["reject_reason"],
                category=RejectionCategory.AUTHORITY_REJECTION,
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
    )
