"""Immutable runtime-record conflict errors.

Runtime record identifiers are content/semantic identities. Replaying an
identical record is idempotent; reusing an identifier for different effective
content is a boundary violation and must fail closed.
"""

from __future__ import annotations


class ImmutableRecordConflict(ValueError):
    """Raised when an immutable runtime-record ID is reused with new content."""

    def __init__(self, record_type: str, record_id: str) -> None:
        self.record_type = record_type
        self.record_id = record_id
        super().__init__(
            f"immutable {record_type} conflict for id {record_id!r}: "
            "the existing canonical payload differs"
        )
