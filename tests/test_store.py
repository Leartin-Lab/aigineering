"""Tests for MemoryStore."""

from aigineering.core.store import MemoryStore
from aigineering.protocol.types import Asset, Contract


def test_add_and_get_asset():
    store = MemoryStore()
    asset = Asset(id="asset_123", name="test_asset", content="hello")
    store.add_asset(asset)
    assert store.get_asset("asset_123") == asset
    assert store.get_asset("nonexistent") is None


def test_get_assets_by_name():
    store = MemoryStore()
    store.add_asset(Asset(id="a1", name="report", content="r1"))
    store.add_asset(Asset(id="a2", name="report", content="r2"))
    store.add_asset(Asset(id="a3", name="other", content="o1"))
    results = store.get_assets_by_name("report")
    assert len(results) == 2
    assert {a.id for a in results} == {"a1", "a2"}


def test_has_asset_named():
    store = MemoryStore()
    store.add_asset(Asset(id="a1", name="data_file", content="d"))
    assert store.has_asset_named("data_file")
    assert not store.has_asset_named("missing")


def test_add_and_get_contract():
    store = MemoryStore()
    contract = Contract(id="c1", name="build", outputs=["report"])
    store.add_contract(contract)
    assert store.get_contract("c1") == contract


def test_get_all():
    store = MemoryStore()
    store.add_asset(Asset(id="a1", name="a", content="x"))
    store.add_asset(Asset(id="a2", name="b", content="y"))
    assert len(store.get_all_assets()) == 2
