"""Candidate projection — the commitment boundary where raw output becomes facts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aigineering.core.authority import check_authority
from aigineering.core.ids import asset_id
from aigineering.protocol.types import Asset, Candidate, Contract

if TYPE_CHECKING:
    from aigineering.core.store import MemoryStore


def project_candidate(
    contract: Contract,
    candidate: Candidate,
    store: "MemoryStore",
) -> tuple[list[Asset], list[dict]]:
    fragments: list[dict] = []
    parse_rejected: list[dict] = []

    for line in candidate.raw_output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            parse_rejected.append({
                "name": stripped[:40],
                "content": "",
                "reject_reason": f"unparsable line (no colon separator): '{stripped[:80]}'",
            })
            continue
        name, _, content = stripped.partition(":")
        name = name.strip()
        content = content.strip()
        if not name:
            parse_rejected.append({
                "name": "(empty)",
                "content": content,
                "reject_reason": f"empty asset name in line: '{stripped[:80]}'",
            })
            continue
        if not content:
            parse_rejected.append({
                "name": name,
                "content": "",
                "reject_reason": f"empty content for asset '{name}'",
            })
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

    deduped_fragments: list[dict] = []
    for f in fragments:
        name = f["name"]
        if name in conflict_names:
            if name not in {r["name"] for r in parse_rejected}:
                parse_rejected.append({
                    "name": name,
                    "content": f["content"],
                    "reject_reason": f"duplicate output '{name}' with conflicting content — all instances rejected",
                })
        else:
            deduped_fragments.append(f)

    accepted_dicts, authority_rejected = check_authority(contract, deduped_fragments)
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
        store.add_asset(asset)
        accepted_assets.append(asset)

    all_rejected = parse_rejected + authority_rejected
    return accepted_assets, all_rejected
