"""Tests for StoreProtocol implementations — parametrized across MemoryStore and JsonLStore."""

import pytest

from aigineering.core.authority import ReservedNamespaceError
from aigineering.core.provenance import sign_asset
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.store import JsonLStore, MemoryStore
from aigineering.protocol.types import Asset, Contract


@pytest.fixture(params=["memory", "jsonl"])
def store(request, tmp_path):
    """Parametrized: runs each test on both Memory and JSONL stores."""
    if request.param == "memory":
        return MemoryStore()
    else:
        return JsonLStore(
            str(tmp_path / "assets.jsonl"), str(tmp_path / "contracts.jsonl")
        )


def test_add_and_get_asset(store):
    asset = sign_asset(
        Asset(id="asset_123", name="test_asset", content="hello", origin="test")
    )
    store.add_asset(asset)
    assert store.get_asset("asset_123") == asset
    assert store.get_asset("nonexistent") is None


def test_asset_replay_is_idempotent_but_id_reuse_conflicts(store):
    asset = sign_asset(
        Asset(id="immutable-a", name="fact", content="v1"), signed_by="test"
    )
    store.add_asset(asset)
    store.add_asset(asset)
    assert store.get_all_assets() == [asset]

    changed = sign_asset(
        Asset(id="immutable-a", name="fact", content="v2"), signed_by="test"
    )
    with pytest.raises(ImmutableRecordConflict, match="immutable asset conflict"):
        store.add_asset(changed)
    assert store.get_asset("immutable-a") == asset


def test_reserved_namespace_requires_explicit_system_ingress(store):
    protected = sign_asset(
        Asset(id="protected-a", name="_sys_runtime", content="v1"),
        signed_by="engine",
    )
    with pytest.raises(ReservedNamespaceError):
        store.add_asset(protected)

    store._add_system_asset(protected)
    assert store.get_asset("protected-a") == protected


def test_get_assets_by_name(store):
    store.add_asset(
        sign_asset(Asset(id="a1", name="report", content="r1", origin="test"))
    )
    store.add_asset(
        sign_asset(Asset(id="a2", name="report", content="r2", origin="test"))
    )
    store.add_asset(
        sign_asset(Asset(id="a3", name="other", content="o1", origin="test"))
    )
    results = store.get_assets_by_name("report")
    assert len(results) == 2
    assert {a.id for a in results} == {"a1", "a2"}


def test_has_asset_named(store):
    store.add_asset(
        sign_asset(Asset(id="a1", name="data_file", content="d", origin="test"))
    )
    assert store.has_asset_named("data_file")
    assert not store.has_asset_named("missing")


def test_add_and_get_contract(store):
    contract = Contract(id="c1", name="build", outputs=["report"])
    store.add_contract(contract)
    assert store.get_contract("c1") == contract


def test_contract_replay_is_idempotent_but_id_reuse_conflicts(store):
    contract = Contract(id="immutable-c", name="build", outputs=["report"])
    store.add_contract(contract)
    store.add_contract(contract)
    assert store.get_all_contracts() == [contract]

    changed = Contract(id="immutable-c", name="build", outputs=["other"])
    with pytest.raises(ImmutableRecordConflict, match="immutable contract conflict"):
        store.add_contract(changed)
    assert store.get_contract("immutable-c") == contract


def test_projection_indexes_rebuild_from_contract_facts(store):
    contract = Contract(
        id="indexed-c",
        activation="input_a AND input_b",
        outputs=["report", "summary"],
    )
    store.add_contract(contract)
    expected_digest = store.projection_index_digest()
    assert set(store.get_contracts_waiting_for("input_a")) == {"indexed-c"}
    assert set(store.get_contracts_declaring_output("report")) == {"indexed-c"}

    store._activation_index.clear()
    store._reverse_activation_index.clear()
    store._declared_outputs_index.clear()
    assert store.projection_index_digest() != expected_digest

    store.rebuild_projection_indexes()
    assert store.projection_index_digest() == expected_digest
    assert set(store.get_contracts_waiting_for("input_b")) == {"indexed-c"}
    assert set(store.get_contracts_declaring_output("summary")) == {"indexed-c"}


def test_get_all(store):
    store.add_asset(sign_asset(Asset(id="a1", name="a", content="x", origin="test")))
    store.add_asset(sign_asset(Asset(id="a2", name="b", content="y", origin="test")))
    assert len(store.get_all_assets()) == 2


def test_name_index_accuracy(store):
    store.add_asset(
        sign_asset(Asset(id="a1", name="alpha", content="x", origin="test"))
    )
    store.add_asset(sign_asset(Asset(id="a2", name="beta", content="y", origin="test")))
    store.add_asset(
        sign_asset(Asset(id="a3", name="alpha", content="z", origin="test"))
    )
    results = store.get_assets_by_name("alpha")
    assert len(results) == 2
    assert {a.id for a in results} == {"a1", "a3"}
    results_beta = store.get_assets_by_name("beta")
    assert len(results_beta) == 1
    assert results_beta[0].id == "a2"


def test_created_by_index(store):
    store.add_asset(
        sign_asset(
            Asset(id="a1", name="r1", content="x", created_by="c1", origin="test")
        )
    )
    store.add_asset(
        sign_asset(
            Asset(id="a2", name="r2", content="y", created_by="c1", origin="test")
        )
    )
    store.add_asset(
        sign_asset(
            Asset(id="a3", name="r3", content="z", created_by="c2", origin="test")
        )
    )
    store.add_asset(sign_asset(Asset(id="a4", name="r4", content="w", origin="test")))
    results_c1 = store.get_assets_by_contract("c1")
    assert len(results_c1) == 2
    assert {a.id for a in results_c1} == {"a1", "a2"}
    results_c2 = store.get_assets_by_contract("c2")
    assert len(results_c2) == 1
    assert results_c2[0].id == "a3"
    results_none = store.get_assets_by_contract("c3")
    assert results_none == []
