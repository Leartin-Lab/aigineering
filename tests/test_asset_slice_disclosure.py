"""Sliced Assets retain the source's existing disclosure classification."""

from dataclasses import replace

import pytest
from click.testing import CliRunner

import aigineering.cli.asset as asset_cli
from aigineering.core.asset_versions import create_slice_asset
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.disclosure import redact_for_disclosure, compute_disclosure
from aigineering.core.ids import contract_from_fields
from aigineering.core.query_projection import StoreQueryProjection
from aigineering.core.sqlite_store import SQLiteStore
from conftest import candidate_runtime


@pytest.mark.parametrize("disclosure_view", ["redacted", "transformed"])
def test_slices_inherit_non_original_view_and_are_redacted_on_reads(
    disclosure_view, monkeypatch, tmp_path
):
    store = SQLiteStore(tmp_path / "store.db")
    runtime = candidate_runtime(store)
    marker = f"private-{disclosure_view}-slice-marker"
    source = runtime.accept_asset(
        replace(
            build_control_plane_asset(name="source", content=marker),
            disclosure_view=disclosure_view,
        )
    )
    source_before = store.get_asset(source.id)

    sliced_proposal = create_slice_asset(
        source, slice_name="source-excerpt", range_spec=f"chars:0-{len(marker)}"
    )
    assert sliced_proposal.disclosure_view == disclosure_view
    sliced = runtime.accept_asset(sliced_proposal)

    assert sliced.disclosure_view == disclosure_view
    assert sliced.promptable is source.promptable
    assert sliced.content == marker
    assert redact_for_disclosure(sliced).content == "[redacted]"
    assert store.get_asset(source.id) == source_before

    monkeypatch.setattr(asset_cli, "_persistent_store", lambda: store)
    monkeypatch.setattr(
        asset_cli, "_query_projection", lambda _store: StoreQueryProjection(_store)
    )
    monkeypatch.setattr("aigineering.cli.infrastructure.require_redis", lambda: None)
    result = CliRunner().invoke(asset_cli.asset_show, ["--id", sliced.id])
    assert result.exit_code == 0, result.output
    assert marker not in result.output
    assert "[redacted]" in result.output


def test_nonpromptable_original_slice_stays_operator_visible_but_not_disclosed(
    tmp_path,
):
    store = SQLiteStore(tmp_path / "store.db")
    runtime = candidate_runtime(store)
    source = runtime.accept_asset(
        build_control_plane_asset(
            name="operator-source",
            content="private operator-only source",
            promptable=False,
        )
    )
    source_before = store.get_asset(source.id)
    sliced = runtime.accept_asset(
        create_slice_asset(
            source, slice_name="operator-excerpt", range_spec="chars:0-23"
        )
    )

    assert sliced.promptable is False
    assert sliced.disclosure_view == "original"
    contract = contract_from_fields(
        name="worker-task",
        description="Use the named input.",
        inputs=("operator-excerpt",),
        outputs=("result",),
        activation="",
        budget=1,
        tool_scope=(),
        labels=(),
        worker_capabilities=(),
        worker_pools=(),
        delegation_capabilities=(),
        delegation_pools=(),
        origin="human",
        parent_id=None,
        minting_authority=(),
    )
    assert compute_disclosure(contract, store) == []
    assert store.get_asset(source.id) == source_before
