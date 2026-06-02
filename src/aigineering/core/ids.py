"""Deterministic SHA-256 IDs for content-addressed runtime objects."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def asset_id(canonical_content: str) -> str:
    return f"asset_{hash_content(canonical_content)}"


def contract_id(canonical_content: str) -> str:
    return f"contract_{hash_content(canonical_content)}"


def trace_entry_id(
    contract_id: str,
    event_type: str,
    sequence: int,
    parent_id: Optional[str] = None,
) -> str:
    components = {
        "contract_id": contract_id,
        "event_type": event_type,
        "sequence": sequence,
        "parent_id": parent_id,
    }
    canonical = json.dumps(components, sort_keys=True, ensure_ascii=False)
    return f"trace_{hash_content(canonical)}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
