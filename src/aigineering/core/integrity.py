"""Read-only integrity checks for SQLite runtime records and their views.

A passing result means local consistency only. It does not prove that the log
is complete, signatures are authentic, or the database was not rewritten.
Those claims require a trusted external checkpoint and signature verification.
"""

from __future__ import annotations

from typing import Any

from aigineering.protocol.runtime_record import validate_runtime_record


def check_store_integrity(store: Any) -> dict[str, Any]:
    """Check record content addresses and materialized views on one snapshot."""
    snapshot = None
    records = []
    invalid_records = 0
    materializations_match: bool | None = None
    error_type: str | None = None
    checked: list[str] = []
    try:
        snapshot = store.snapshot()
        records = snapshot.scan_runtime_records()
        for _, record in records:
            try:
                validate_runtime_record(record)
            except (TypeError, ValueError, KeyError):
                invalid_records += 1
        checked.append("runtime_record_content_addresses")

        if invalid_records == 0:
            before = snapshot.runtime_materialization_digest()
            checked.append("materialized_views_against_runtime_records")
            try:
                snapshot.rebuild_runtime_materializations(strict=True)
                materializations_match = (
                    before == snapshot.runtime_materialization_digest()
                )
            except Exception as error:
                # A rebuild conflict is itself evidence of local view drift.
                error_type = type(error).__name__
                materializations_match = False
    except Exception as error:
        # Exception text can embed record contents, including sealed data.
        error_type = type(error).__name__
        materializations_match = False
    finally:
        if snapshot is not None:
            snapshot.close()

    consistent = invalid_records == 0 and materializations_match is True
    not_checked = [
        "runtime_record_log_completeness",
        "external_checkpoint_or_coordinated_database_rewrite",
        "actor_signature_authenticity",
    ]
    if "runtime_record_content_addresses" not in checked:
        not_checked.append("runtime_record_content_addresses")
    if "materialized_views_against_runtime_records" not in checked:
        not_checked.append("materialized_views_against_runtime_records")
    return {
        "status": "passed" if consistent else "failed",
        "consistent": consistent,
        "scope": "local_runtime_record_and_materialization_consistency",
        "checked": checked,
        "not_checked": not_checked,
        "runtime_record_count": len(records),
        "invalid_runtime_record_count": invalid_records,
        "materializations_match": materializations_match,
        "error_type": error_type,
    }
