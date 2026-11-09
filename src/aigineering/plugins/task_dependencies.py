"""Pure signed effects for claim-gating task dependencies."""

from __future__ import annotations

from collections.abc import Iterable

from aigineering.core.claim_constraints import (
    PLACED,
    RELEASED,
)
from aigineering.protocol.runtime_record import RuntimeRecord


def project_task_dependencies(
    runtime_records: Iterable[RuntimeRecord], *, contract_id: str | None = None
) -> tuple[RuntimeRecord, ...]:
    """Return immutable dependency placements and releases in supplied order."""
    return tuple(
        record
        for record in runtime_records
        if record.record_type in {PLACED, RELEASED}
        and (contract_id is None or record.payload.get("contract_id") == contract_id)
    )


__all__ = ["project_task_dependencies"]
