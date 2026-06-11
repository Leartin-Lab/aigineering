"""Tests for Asset retention fields (keep_flag, tombstoned, tombstoned_at)."""

from aigineering.core.store import JsonLStore
from aigineering.protocol.types import Asset


def test_keep_flag_defaults_to_false():
    """New Asset has keep_flag=False by default."""
    asset = Asset(id="a1", name="test", content="hello")
    assert asset.keep_flag is False


def test_tombstoned_defaults_to_false():
    """New Asset has tombstoned=False by default."""
    asset = Asset(id="a2", name="test", content="hello")
    assert asset.tombstoned is False


def test_tombstoned_at_defaults_to_none():
    """New Asset has tombstoned_at=None by default."""
    asset = Asset(id="a3", name="test", content="hello")
    assert asset.tombstoned_at is None


def test_keep_flag_can_be_true():
    """Asset(keep_flag=True) stores the value correctly."""
    asset = Asset(id="a4", name="test", content="hello", keep_flag=True)
    assert asset.keep_flag is True
    assert asset.tombstoned is False
    assert asset.tombstoned_at is None


def test_tombstoned_can_be_true():
    """Asset(tombstoned=True) stores the value correctly."""
    asset = Asset(id="a5", name="test", content="hello", tombstoned=True)
    assert asset.tombstoned is True
    assert asset.keep_flag is False
    assert asset.tombstoned_at is None


def test_retention_fields_persist_in_store(tmp_path):
    """Asset retention fields survive a JsonLStore write/read cycle."""
    store = JsonLStore(
        str(tmp_path / "assets.jsonl"),
        str(tmp_path / "contracts.jsonl"),
    )

    asset = Asset(
        id="asset_ret",
        name="retained",
        content="valuable",
        keep_flag=True,
        tombstoned=True,
        tombstoned_at="2026-06-11T12:00:00Z",
    )

    store.add_asset(asset)

    reopened = JsonLStore(
        str(tmp_path / "assets.jsonl"),
        str(tmp_path / "contracts.jsonl"),
    )

    retrieved = reopened.get_asset("asset_ret")
    assert retrieved is not None
    assert retrieved.keep_flag is True
    assert retrieved.tombstoned is True
    assert retrieved.tombstoned_at == "2026-06-11T12:00:00Z"


def test_retention_fields_not_in_canonical_do_not_affect_id():
    """Retention fields excluded from canonical serialization preserve
    deterministic asset ID stability."""
    a1 = Asset(id="a6", name="ret", content="data")
    a2 = Asset(
        id="a6",
        name="ret",
        content="data",
        keep_flag=True,
        tombstoned=True,
        tombstoned_at="2026-06-11T12:00:00Z",
    )
    assert a1.id == a2.id
    assert a1.name == a2.name
    assert a1.content == a2.content
    # retention fields differ but identity is the same
    assert a1.keep_flag is False
    assert a2.keep_flag is True
