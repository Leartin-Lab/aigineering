"""Tests for StoreProtocol implementations — parametrized across MemoryStore and JsonLStore."""

import pytest

from aigineering.core.store import JsonLStore, MemoryStore
from aigineering.protocol.types import Asset, Contract


@pytest.fixture(params=["memory", "jsonl"])
def store(request, tmp_path):
    """Parametrized: runs each test on both Memory and JSONL stores."""
    if request.param == "memory":
        return MemoryStore()
    else:
        return JsonLStore(str(tmp_path / "assets.jsonl"), str(tmp_path / "contracts.jsonl"))


def test_add_and_get_asset(store):
    asset = Asset(id="asset_123", name="test_asset", content="hello")
    store.add_asset(asset)
    assert store.get_asset("asset_123") == asset
    assert store.get_asset("nonexistent") is None


def test_get_assets_by_name(store):
    store.add_asset(Asset(id="a1", name="report", content="r1"))
    store.add_asset(Asset(id="a2", name="report", content="r2"))
    store.add_asset(Asset(id="a3", name="other", content="o1"))
    results = store.get_assets_by_name("report")
    assert len(results) == 2
    assert {a.id for a in results} == {"a1", "a2"}


def test_has_asset_named(store):
    store.add_asset(Asset(id="a1", name="data_file", content="d"))
    assert store.has_asset_named("data_file")
    assert not store.has_asset_named("missing")


def test_add_and_get_contract(store):
    contract = Contract(id="c1", name="build", outputs=["report"])
    store.add_contract(contract)
    assert store.get_contract("c1") == contract


def test_get_all(store):
    store.add_asset(Asset(id="a1", name="a", content="x"))
    store.add_asset(Asset(id="a2", name="b", content="y"))
    assert len(store.get_all_assets()) == 2


def test_name_index_accuracy(store):
    store.add_asset(Asset(id="a1", name="alpha", content="x"))
    store.add_asset(Asset(id="a2", name="beta", content="y"))
    store.add_asset(Asset(id="a3", name="alpha", content="z"))
    results = store.get_assets_by_name("alpha")
    assert len(results) == 2
    assert {a.id for a in results} == {"a1", "a3"}
    results_beta = store.get_assets_by_name("beta")
    assert len(results_beta) == 1
    assert results_beta[0].id == "a2"


def test_created_by_index(store):
    store.add_asset(Asset(id="a1", name="r1", content="x", created_by="c1"))
    store.add_asset(Asset(id="a2", name="r2", content="y", created_by="c1"))
    store.add_asset(Asset(id="a3", name="r3", content="z", created_by="c2"))
    store.add_asset(Asset(id="a4", name="r4", content="w"))
    results_c1 = store.get_assets_by_contract("c1")
    assert len(results_c1) == 2
    assert {a.id for a in results_c1} == {"a1", "a2"}
    results_c2 = store.get_assets_by_contract("c2")
    assert len(results_c2) == 1
    assert results_c2[0].id == "a3"
    results_none = store.get_assets_by_contract("c3")
    assert results_none == []
