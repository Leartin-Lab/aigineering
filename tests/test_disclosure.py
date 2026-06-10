"""Tests for asset disclosure policy."""

from aigineering.agent.mock import MockWorker
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.engine import Engine
from aigineering.core.ids import asset_id, contract_id
from aigineering.core.labels import Label
from aigineering.core.store import MemoryStore
from aigineering.core.trace import TraceStore
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import asset_to_canonical, contract_to_canonical


def _asset(
    name: str,
    content: str,
    *,
    promptable: bool = True,
    disclosure_view: str = "original",
) -> Asset:
    draft = Asset(
        id="",
        name=name,
        content=content,
        promptable=promptable,
        disclosure_view=disclosure_view,
    )
    return Asset(
        id=asset_id(asset_to_canonical(draft)),
        name=name,
        content=content,
        promptable=promptable,
        disclosure_view=disclosure_view,
    )


def _contract(**kwargs) -> Contract:
    draft = Contract(id="", **kwargs)
    return Contract(id=contract_id(contract_to_canonical(draft)), **kwargs)


def test_non_promptable_input_asset_is_not_disclosed():
    store = MemoryStore()
    sealed = _asset("secret", "do not disclose", promptable=False, disclosure_view="sealed")
    store.add_asset(sealed)
    contract = _contract(name="task", inputs=["secret"], outputs=["result"])

    assert compute_disclosure(contract, store) == []
    assert store.get_asset(sealed.id) == sealed


def test_engine_does_not_disclose_non_promptable_label_asset():
    store = MemoryStore()
    trace = TraceStore()
    worker = MockWorker()
    sealed_skill = _asset(
        "_skill_secret",
        "sealed procedure",
        promptable=False,
        disclosure_view="sealed",
    )
    input_asset = _asset("input", "input content")
    store.add_asset(sealed_skill)
    store.add_asset(input_asset)

    contract = _contract(
        name="task",
        inputs=["input"],
        outputs=["result"],
        activation="input",
        labels=["secret_label"],
        budget=1,
    )
    worker.set_output("task", "result: ok")

    engine = Engine(
        store,
        worker,
        trace,
        labels={"secret_label": Label(name="secret_label", assets=["_skill_secret"])},
    )
    engine.add_contract(contract)
    engine.run()

    label_entries = trace.get_by_event_type("label_resolved")
    assert sealed_skill.id in label_entries[0].disclosed_assets

    disclosure_entries = trace.get_by_event_type("disclosure")
    assert input_asset.id in disclosure_entries[0].disclosed_assets
    assert sealed_skill.id not in disclosure_entries[0].disclosed_assets
    assert store.get_asset(sealed_skill.id) == sealed_skill
