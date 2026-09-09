"""Backward-compatible identity imports.

Canonical identity belongs to the protocol layer. This module remains as a
stable public import path and deliberately re-exports the original objects.
"""

from aigineering.protocol.identity import *  # noqa: F403
from aigineering.protocol.identity import __all__ as __all__
