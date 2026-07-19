"""Release-gate regressions for operational runtime boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import candidate_runtime

from aigineering.core.asset_versions import create_replacement_claim
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.ids import hash_asset_content
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Asset


def _asset(name: str, content: str) -> Asset:
    return Asset(
        id=hash_asset_content(name, content),
        name=name,
        content=content,
    )


def test_sqlite_ingress_replacement_claim_rolls_back_with_trace_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement claim cannot persist without its audit trace."""
    store = SQLiteStore(":memory:")
    ingress = candidate_runtime(store)
    source = ingress.accept_asset(_asset("report", "v1"), source="test")
    replacement = ingress.accept_asset(_asset("report", "v2"), source="test")
    claim = create_replacement_claim(source.id, replacement.id)

    def fail_trace(_entry: object) -> None:
        raise RuntimeError("injected trace failure")

    monkeypatch.setattr(store, "_insert_trace_entry", fail_trace)
    with pytest.raises(RuntimeError, match="injected trace failure"):
        ingress.accept_replacement_claim(claim, source="test")

    assert store.get_claims_for_asset(source.id) == []
    assert store.get_by_event_type("replacement_claim_created") == []


def test_commitment_has_no_claimless_compatibility_ingress() -> None:
    """Only typed Candidate commitment remains as a production write owner."""
    assert not Path("src/aigineering/core/runtime_ingress.py").exists()
    assert not hasattr(CandidateCommitter, "accept_candidate_submission")


def test_server_runtime_composition_does_not_import_cli_private_modules() -> None:
    source = Path("src/aigineering/server/app.py").read_text()

    assert "aigineering.cli" not in source
    assert "aigineering.application" in source
