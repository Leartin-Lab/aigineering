"""Crash injection utilities for testing recovery semantics.

Crash injection is **disabled by default** and must be explicitly
enabled by setting the environment variable ``AIG_ENABLE_CRASH_INJECTION=1``
before any crash points will fire.  In production (env var unset) every
call to :func:`check_crash_point` returns immediately with zero overhead
beyond a single ``os.getenv`` lookup.
"""

from __future__ import annotations

import os


def _crash_injection_enabled() -> bool:
    """Return True when crash injection has been explicitly enabled."""
    return os.getenv("AIG_ENABLE_CRASH_INJECTION", "") == "1"


def check_crash_point(name: str) -> None:
    """Call ``os._exit(1)`` when *name* matches ``$AIG_CRASH_POINT`` AND
    crash injection is enabled via ``$AIG_ENABLE_CRASH_INJECTION=1``.

    This function is intentionally minimal so it can be called from
    anywhere in the runtime without adding import-side effects.  In
    production (no env var set) it returns immediately.
    """
    if not _crash_injection_enabled():
        return
    target = os.getenv("AIG_CRASH_POINT", "")
    if target and target == name:
        os._exit(1)
