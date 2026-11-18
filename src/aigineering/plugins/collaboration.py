"""Pure projection for signed, task-scoped collaboration comments."""

from __future__ import annotations

from collections.abc import Iterable

from aigineering.core.claim_holds import open_claim_holds
from aigineering.protocol.runtime_record import RuntimeRecord


def project_task_comments(
    runtime_records: Iterable[RuntimeRecord], *, contract_id: str | None = None
) -> tuple[RuntimeRecord, ...]:
    """Return committed comments in the supplied durable record order."""
    return tuple(
        record
        for record in runtime_records
        if record.record_type == "task.commented"
        and (contract_id is None or record.payload.get("contract_id") == contract_id)
    )


def project_task_holds(
    runtime_records: Iterable[RuntimeRecord], *, contract_id: str | None = None
) -> tuple[RuntimeRecord, ...]:
    """Return current open generic claim holds for task-facing projections."""
    return open_claim_holds(runtime_records, contract_id=contract_id)
