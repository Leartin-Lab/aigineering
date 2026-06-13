"""Tests for dual-hash asset model (definition_hash + content_hash)."""

import pytest

from aigineering.core.provenance import sign_asset
from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.core.store import JsonLStore, MemoryStore
from aigineering.protocol.types import Asset


@pytest.fixture(params=["memory", "jsonl"])
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryStore()
    else:
        return JsonLStore(str(tmp_path / "assets.jsonl"), str(tmp_path / "contracts.jsonl"))


def test_definition_hash_is_set_on_creation():
    """Asset created with definition_hash and content_hash populated."""
    name = "test_report"
    content = "Test report content v1"
    asset = Asset(
        id=hash_asset_content(name, content),
        name=name,
        content=content,
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, content),
    )
    assert asset.definition_hash == hash_asset_definition(name)
    assert asset.content_hash == hash_asset_content(name, content)
    assert asset.definition_hash.startswith("def:")
    assert asset.content_hash.startswith("content:")


def test_definition_hash_backward_compatible_defaults():
    """Assets created without definition_hash/defaults have empty string values."""
    asset = Asset(id="a1", name="legacy", content="old")
    assert asset.definition_hash == ""
    assert asset.content_hash == ""


def test_version_chain_lookup(store):
    """Two assets with same definition_hash → get_assets_by_definition returns both."""
    name = "versioned_report"
    content_v1 = "Report v1"
    content_v2 = "Report v2"

    asset1 = Asset(
        id=hash_asset_content(name, content_v1),
        name=name,
        content=content_v1,
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, content_v1),
    )
    asset2 = Asset(
        id=hash_asset_content(name, content_v2),
        name=name,
        content=content_v2,
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, content_v2),
    )

    store.add_asset(asset1)
    store.add_asset(asset2)

    def_hash = hash_asset_definition(name)
    results = store.get_assets_by_definition(def_hash)
    assert len(results) == 2
    result_ids = {a.id for a in results}
    assert asset1.id in result_ids
    assert asset2.id in result_ids


def test_get_latest_asset(store):
    """get_latest_asset returns most recently added asset for a definition."""
    name = "latest_test"
    v1 = "Version 1"
    v2 = "Version 2"
    v3 = "Version 3"

    asset1 = Asset(
        id=hash_asset_content(name, v1),
        name=name,
        content=v1,
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, v1),
    )
    asset2 = Asset(
        id=hash_asset_content(name, v2),
        name=name,
        content=v2,
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, v2),
    )
    asset3 = Asset(
        id=hash_asset_content(name, v3),
        name=name,
        content=v3,
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, v3),
    )

    store.add_asset(asset1)
    store.add_asset(asset2)
    store.add_asset(asset3)

    def_hash = hash_asset_definition(name)
    latest = store.get_latest_asset(def_hash)
    assert latest is not None
    assert latest.id == asset3.id
    assert latest.content == v3


def test_get_latest_asset_returns_none_for_unknown_definition(store):
    """get_latest_asset returns None when no assets match the definition hash."""
    result = store.get_latest_asset("def:nonexistent")
    assert result is None


def test_batch_verify_content_hashes(store):
    """Verify all assets under a definition have valid content hashes."""
    name = "batch_report"

    for i in range(1, 6):
        content = f"Batch report version {i}"
        asset = Asset(
            id=hash_asset_content(name, content),
            name=name,
            content=content,
            definition_hash=hash_asset_definition(name),
            content_hash=hash_asset_content(name, content),
        )
        store.add_asset(asset)

    def_hash = hash_asset_definition(name)
    assets = store.get_assets_by_definition(def_hash)
    assert len(assets) == 5

    for asset in assets:
        expected_content_hash = hash_asset_content(asset.name, asset.content)
        assert asset.content_hash == expected_content_hash, (
            f"Asset {asset.id} has content_hash {asset.content_hash!r} "
            f"but expected {expected_content_hash!r}"
        )
        expected_def_hash = hash_asset_definition(asset.name)
        assert asset.definition_hash == expected_def_hash, (
            f"Asset {asset.id} has definition_hash {asset.definition_hash!r} "
            f"but expected {expected_def_hash!r}"
        )


def test_different_definitions_are_isolated(store):
    """Assets with different names/definitions don't mix in version chain lookups."""
    name_a = "asset_a"
    name_b = "asset_b"

    asset_a = Asset(
        id=hash_asset_content(name_a, "content a"),
        name=name_a,
        content="content a",
        definition_hash=hash_asset_definition(name_a),
        content_hash=hash_asset_content(name_a, "content a"),
    )
    asset_b = Asset(
        id=hash_asset_content(name_b, "content b"),
        name=name_b,
        content="content b",
        definition_hash=hash_asset_definition(name_b),
        content_hash=hash_asset_content(name_b, "content b"),
    )

    store.add_asset(asset_a)
    store.add_asset(asset_b)

    results_a = store.get_assets_by_definition(hash_asset_definition(name_a))
    assert len(results_a) == 1
    assert results_a[0].id == asset_a.id

    results_b = store.get_assets_by_definition(hash_asset_definition(name_b))
    assert len(results_b) == 1
    assert results_b[0].id == asset_b.id
