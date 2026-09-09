"""Legacy JSONL projection adapter.

JSONL is retained for compatibility and replay fixtures; SQLite remains the
authoritative runtime store.
"""

from aigineering.core.store import JsonLStore

__all__ = ["JsonLStore"]
