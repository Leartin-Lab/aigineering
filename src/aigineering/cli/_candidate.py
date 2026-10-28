"""Compatibility re-exports for local CLI Candidate publication."""

from aigineering.local_publication import (
    commit_local_effect,
    commit_local_effects,
    require_accepted,
)

__all__ = ["commit_local_effect", "commit_local_effects", "require_accepted"]
