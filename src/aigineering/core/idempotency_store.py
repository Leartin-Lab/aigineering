"""Idempotency store for operational candidate submission.

Maps (contract_id, idempotency_key) → result to enable safe retry without
duplicate asset creation.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)


class IdempotencyStore:
    """JSONL-backed store for idempotency key → submission result mapping.

    Each entry is a single JSONL line::

        {"contract_id":"...","idempotency_key":"...","result":{...}}
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path: Optional[str] = path
        self._map: dict[tuple[str, str], dict] = {}
        if path is not None:
            parent = Path(path).parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            self._load()

    def _load(self) -> None:
        if self._path is None or not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                cid = data.get("contract_id", "")
                key = data.get("idempotency_key", "")
                result = data.get("result")
                if cid and key and isinstance(result, dict):
                    self._map[(cid, key)] = result

    def _write(self, contract_id: str, idempotency_key: str, result: dict) -> None:
        if self._path is None:
            return
        line = json.dumps(
            {
                "contract_id": contract_id,
                "idempotency_key": idempotency_key,
                "result": result,
            },
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                _logger.warning("fsync failed for %s", self._path)

    def get(self, contract_id: str, idempotency_key: str) -> Optional[dict]:
        """Return the cached result, or *None*."""
        return self._map.get((contract_id, idempotency_key))

    def set(self, contract_id: str, idempotency_key: str, result: dict) -> None:
        """Store a result under (contract_id, idempotency_key)."""
        self._map[(contract_id, idempotency_key)] = result
        self._write(contract_id, idempotency_key, result)

    def has_any(self, contract_id: str) -> bool:
        """Return *True* if *contract_id* has at least one idempotency record."""
        return any(k[0] == contract_id for k in self._map)
