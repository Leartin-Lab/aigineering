"""Release-gate regressions for operational runtime boundaries."""

from __future__ import annotations

import pytest

from aigineering.core.asset_versions import create_replacement_claim
from aigineering.core.ids import hash_asset_content, hash_contract_v2
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.protocol.types import Asset, Candidate, Contract


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
    ingress = RuntimeIngress(store, store)
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


def test_sqlite_rejects_claimless_programmatic_candidate_submission() -> None:
    """Operational SQLite candidates cannot bypass worker-package binding."""
    store = SQLiteStore(":memory:")
    ingress = RuntimeIngress(store, store)
    contract = Contract(
        id=hash_contract_v2(
            name="claim-bound",
            description="",
            inputs=[],
            outputs=["result"],
            activation="",
            budget=1,
            tool_scope=[],
            labels=[],
            origin="human",
        ),
        name="claim-bound",
        outputs=("result",),
        budget=1,
    )
    ingress.accept_contract(contract)

    with pytest.raises(RuntimeError, match="claim-bound"):
        ingress.accept_candidate_submission(
            contract,
            Candidate(worker_id="bypass", raw_output="result: no"),
        )


def test_memory_store_rejects_claimless_programmatic_candidate_submission() -> None:
    """Test stores cannot weaken the operational commitment boundary."""
    store = MemoryStore()
    ingress = RuntimeIngress(store, store)
    contract = Contract(id="contract:memory", outputs=("result",), budget=1)

    with pytest.raises(RuntimeError, match="claim-bound"):
        ingress.accept_candidate_submission(
            contract,
            Candidate(worker_id="bypass", raw_output="result: no"),
        )
