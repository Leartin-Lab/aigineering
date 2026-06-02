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
    for line in candidate.raw_output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        name, _, content = stripped.partition(":")
        name = name.strip()
        content = content.strip()
        if name and content:
            fragments.append({"name": name, "content": content})

    accepted_dicts, rejected_dicts = check_authority(contract, fragments)
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

    return accepted_assets, rejected_dicts
