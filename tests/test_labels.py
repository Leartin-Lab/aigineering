"""Tests for label-based asset injection."""

import json

from aigineering.agent.mock import MockWorker
from aigineering.core.engine import Engine
from aigineering.core.ids import hash_asset_content, hash_contract
from aigineering.core.labels import Label, resolve_contract_labels
from aigineering.core.provenance import sign_asset
from aigineering.core.store import MemoryStore
from aigineering.core.trace import TraceStore
from aigineering.protocol.types import Asset, Contract


def _asset(name: str, content: str, origin: str = "human") -> Asset:
    return Asset(
        id=hash_asset_content(name, content),
        name=name,
        content=content,
        origin=origin,
    )


def _contract(**kwargs) -> Contract:
    contract = Contract(id="", **kwargs)
    return Contract(
        id=hash_contract(
            name=contract.name,
            description=contract.description,
            inputs=list(contract.inputs),
            outputs=list(contract.outputs),
            activation=contract.activation,
            budget=contract.budget,
            tool_scope=list(contract.tool_scope),
            labels=list(contract.labels),
            origin=contract.origin,
        ),
        **kwargs,
    )


def test_label_injects_existing_asset():
    store = MemoryStore()
    skill = _asset("_skill_review", "review procedure", origin="skill")
    store.add_asset(sign_asset(skill))
    contract = _contract(name="review", labels=["reviewer"], outputs=["result"])

    result = resolve_contract_labels(
        contract,
        {"reviewer": Label(name="reviewer", assets=["_skill_review"])},
        store,
    )

    assert result.injected_assets == [skill]
    assert result.placeholder_assets == []


def test_label_missing_dependency_creates_placeholder_asset():
    store = MemoryStore()
    contract = _contract(name="review", labels=["reviewer"], outputs=["result"])

    result = resolve_contract_labels(
        contract,
        {"reviewer": Label(name="reviewer", assets=["_skill_missing"])},
        store,
    )

    assert len(result.injected_assets) == 1
    placeholder = result.placeholder_assets[0]
    assert placeholder.name == "_skill_missing"
    assert placeholder.origin == "label_placeholder"
    assert json.loads(placeholder.content)["placeholder"] is True
    assert store.get_asset(placeholder.id) == placeholder


def test_engine_discloses_label_injected_assets_and_traces_resolution():
    store = MemoryStore()
    trace = TraceStore()
    worker = MockWorker()
    skill = _asset("_skill_review", "review procedure", origin="skill")
    input_asset = _asset("input", "input content")
    store.add_asset(sign_asset(skill))
    store.add_asset(sign_asset(input_asset))

    contract = _contract(
        name="review",
        inputs=["input"],
        outputs=["result"],
        activation="input",
        labels=["reviewer"],
        budget=1,
    )
    worker.set_output("review", "result: ok")

    engine = Engine(
        store,
        worker,
        trace,
        labels={"reviewer": Label(name="reviewer", assets=["_skill_review"])},
    )
    engine.add_contract(contract)
    engine.run()

    label_entries = trace.get_by_event_type("label_resolved")
    assert len(label_entries) == 1
    assert skill.id in label_entries[0].disclosed_assets
    assert label_entries[0].relation_type == "label"
    assert label_entries[0].relation_target == "reviewer"

    disclosure_entries = trace.get_by_event_type("disclosure")
    assert len(disclosure_entries) == 1
    assert input_asset.id in disclosure_entries[0].disclosed_assets
    assert skill.id in disclosure_entries[0].disclosed_assets
