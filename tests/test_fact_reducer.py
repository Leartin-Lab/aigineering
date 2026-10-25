"""Focused FactReducer activation projection behavior."""

from __future__ import annotations

import pytest

from aigineering.core.fact_reducer import FactReducer
from aigineering.core.provenance import sign_asset
from aigineering.core.store import MemoryStore
from aigineering.protocol.types import Asset, Contract


@pytest.mark.parametrize("batch_size", [2, 8])
def test_activation_reads_assets_once_per_nonempty_batch(batch_size, monkeypatch):
    store = MemoryStore()
    store.add_asset(
        sign_asset(
            Asset(id="committed", name="committed", content="value"),
            signed_by="human",
        )
    )
    store.add_contract(Contract(id="and", activation="committed AND pending_0"))
    store.add_contract(Contract(id="or", activation="pending_0 OR absent"))
    store.add_contract(Contract(id="missing", activation="absent AND missing"))
    assets = tuple(
        Asset(id=f"pending-{index}", name=f"pending_{index}", content="value")
        for index in range(batch_size)
    )
    reads = 0
    original_get_all_assets = store.get_all_assets

    def counted_get_all_assets():
        nonlocal reads
        reads += 1
        return original_get_all_assets()

    monkeypatch.setattr(store, "get_all_assets", counted_get_all_assets)
    events = FactReducer(store).on_assets_created(assets)

    activations = [
        (event.contract_id, event.asset_name)
        for event in events
        if event.type == "activation_active"
    ]
    assert activations == [("and", "pending_0"), ("or", "pending_0")]
    assert reads <= 1


def test_activation_batch_order_does_not_change_available_names():
    assets = (
        Asset(id="a", name="left", content="value"),
        Asset(id="b", name="right", content="value"),
    )
    contract = Contract(id="both", activation="left AND right")
    snapshots = []

    for batch in (assets, tuple(reversed(assets))):
        store = MemoryStore()
        store.add_contract(contract)
        snapshots.append(
            [
                (event.type, event.contract_id, event.asset_name)
                for event in FactReducer(store).on_assets_created(batch)
            ]
        )

    assert snapshots[0] == [("activation_active", "both", "left")]
    assert snapshots[1] == [("activation_active", "both", "right")]


@pytest.mark.parametrize("reverse", [False, True])
def test_invalid_pending_output_name_still_activates_but_does_not_complete(reverse):
    store = MemoryStore()
    contract = Contract(
        id="mixed-output",
        name="mixed-output",
        outputs=("business_result", "auxiliary_result"),
        activation="auxiliary_result",
    )
    store.add_contract(contract)
    business = Asset(
        id="business",
        name="business_result",
        content="accepted",
        origin="human",
    )
    observation = Asset(
        id="observation",
        name="auxiliary_result",
        content="observed",
        origin="tool",
    )
    assets = (observation, business) if reverse else (business, observation)

    events = FactReducer(store).on_assets_created(assets)

    assert ("activation_active", contract.id) in {
        (event.type, event.contract_id) for event in events
    }
    assert ("contract_complete", contract.id) not in {
        (event.type, event.contract_id) for event in events
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_same_name_valid_pending_output_is_not_hidden_by_observation(reverse):
    store = MemoryStore()
    contract = Contract(id="same-name", outputs=("result",))
    store.add_contract(contract)
    business = Asset(
        id="business",
        name="result",
        content="accepted",
        origin="human",
    )
    observation = Asset(
        id="observation",
        name="result",
        content="observed",
        origin="tool",
    )
    assets = (observation, business) if reverse else (business, observation)

    events = FactReducer(store).on_assets_created(assets)

    assert ("contract_complete", contract.id) in {
        (event.type, event.contract_id) for event in events
    }


def test_empty_batch_does_not_read_assets():
    store = MemoryStore()
    reads = 0
    original_get_all_assets = store.get_all_assets

    def counted_get_all_assets():
        nonlocal reads
        reads += 1
        return original_get_all_assets()

    store.get_all_assets = counted_get_all_assets

    assert FactReducer(store).on_assets_created(()) == []
    assert reads == 0
