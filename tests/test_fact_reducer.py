"""Focused FactReducer activation projection behavior."""

from __future__ import annotations

from dataclasses import replace

import pytest

from aigineering.core.fact_reducer import FactReducer
import aigineering.core.fact_reducer as fact_reducer_module
from aigineering.core.lifecycle_facts import create_terminal_record
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


def test_dispatch_index_is_one_contract_pass_with_sparse_ordered_buckets(monkeypatch):
    contracts = tuple(
        Contract(
            id=f"c-{index:02}",
            activation=(f"asset-{index} AND absent" if index < 2 else "never"),
            outputs=((f"asset-{index}", "missing") if index < 2 else ("other",)),
        )
        for index in range(8)
    )
    store = MemoryStore()
    for contract in contracts:
        store.add_contract(contract)
    assets = tuple(
        Asset(id=f"a-{index}", name=f"asset-{index}", content="value")
        for index in range(2)
    )

    original_builder = getattr(fact_reducer_module, "_build_asset_dispatch_index", None)
    assert callable(original_builder), "batch dispatch index builder is missing"
    observed = []

    class CountingContracts:
        def __init__(self, items):
            self.items = items
            self.yields = 0

        def __iter__(self):
            for item in self.items:
                self.yields += 1
                yield item

    def counted_builder(items, batch):
        counted = CountingContracts(items)
        result = original_builder(counted, batch)
        observed.append((counted.yields, result))
        return result

    monkeypatch.setattr(
        fact_reducer_module, "_build_asset_dispatch_index", counted_builder
    )
    events = FactReducer(store).on_assets_created(assets)

    assert len(observed) == 1
    assert observed[0][0] == len(contracts)
    index = observed[0][1]
    assert tuple(item.id for _, item in index.activation_by_raw_token["asset-0"]) == (
        "c-00",
    )
    assert tuple(item.id for item in index.outputs_by_name["asset-1"]) == ("c-01",)
    assert [(event.type, event.contract_id, event.asset_name) for event in events] == [
        ("output_satisfied", "c-00", "asset-0"),
        ("output_satisfied", "c-01", "asset-1"),
    ]


def test_empty_batch_does_not_build_any_dispatch_index(monkeypatch):
    store = MemoryStore()
    contract = Contract(id="malformed", activation="valid")
    store.add_contract(contract)
    # Simulate a legacy-decoded malformed payload that cannot be added through
    # the current validating Store API.
    store.contracts[contract.id] = replace(contract, activation=object())
    original_builder = fact_reducer_module._build_asset_dispatch_index
    builds = 0

    def forbidden_builder(*args, **kwargs):
        nonlocal builds
        builds += 1
        raise AssertionError("empty batch built a dispatch index")

    monkeypatch.setattr(
        fact_reducer_module,
        "_build_asset_dispatch_index",
        forbidden_builder,
        raising=False,
    )
    assert FactReducer(store).on_assets_created(()) == []
    assert builds == 0
    monkeypatch.setattr(
        fact_reducer_module,
        "_build_asset_dispatch_index",
        original_builder,
        raising=False,
    )
    with pytest.raises(AttributeError, match="strip"):
        FactReducer(store).on_assets_created(
            (Asset(id="a", name="unrelated", content="value"),)
        )
    assert builds == 0


@pytest.mark.parametrize("asset_name", ["AND", "OR"])
def test_raw_operator_word_activation_match_keeps_parser_error(asset_name):
    store = MemoryStore()
    store.add_contract(Contract(id="operator", activation=asset_name))
    reducer = FactReducer(store)
    index = fact_reducer_module._build_asset_dispatch_index(
        store.get_all_contracts(), (Asset(id="a", name=asset_name, content="v"),)
    )
    assert index.activation_candidates(asset_name) == tuple(store.get_all_contracts())
    with pytest.raises(ValueError, match="Unexpected token"):
        reducer.on_assets_created((Asset(id="a", name=asset_name, content="v"),))


def test_activation_parenthesis_and_malformed_error_stay_lazy():
    store = MemoryStore()
    store.add_contract(Contract(id="parenthesized", activation="(alpha)"))
    reducer = FactReducer(store)
    assert (
        reducer.on_assets_created((Asset(id="a", name="(alpha)", content="v"),)) == []
    )

    malformed = MemoryStore()
    malformed.add_contract(Contract(id="malformed", activation="broken AND"))
    malformed_reducer = FactReducer(malformed)
    assert malformed_reducer.on_assets_created(()) == []
    assert (
        malformed_reducer.on_assets_created(
            (Asset(id="b", name="unrelated", content="v"),)
        )
        == []
    )
    with pytest.raises(ValueError, match="Expected asset name"):
        malformed_reducer.on_assets_created(
            (Asset(id="c", name="broken", content="v"),)
        )


def test_terminal_contract_keeps_activation_trace_and_skips_output():
    store = MemoryStore()
    contract = Contract(
        id="closed",
        activation="terminal_asset",
        outputs=("terminal_asset",),
    )
    store.add_contract(contract)
    store.append_runtime_record(create_terminal_record(contract.id, "complete"))

    events = FactReducer(store).on_assets_created(
        (Asset(id="terminal", name="terminal_asset", content="v"),)
    )
    assert [(event.type, event.contract_id) for event in events] == [
        ("activation_active", contract.id)
    ]
