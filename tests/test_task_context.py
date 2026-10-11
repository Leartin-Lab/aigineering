"""Read-only task context view preserves existing disclosure semantics."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from click import Group
from click.testing import CliRunner

from aigineering.cli.task_context import register_task_context_command
from aigineering.core.disclosure import REDACTED_CONTENT
from aigineering.core.control_plane import (
    bind_contract_label_assets,
    build_control_plane_contract,
)
from aigineering.core.ids import (
    contract_from_fields,
    hash_asset_content,
    hash_contract,
    hash_contract_v5,
)
from aigineering.core.provenance import sign_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import create_entry
from aigineering.protocol.types import Asset, Contract
from aigineering.task_context import TaskContextError, task_context
from conftest import candidate_runtime


def _asset(name: str, content: str, **kwargs) -> Asset:
    kwargs.setdefault("origin", "test")
    return sign_asset(
        Asset(
            id=hash_asset_content(name, content),
            name=name,
            content=content,
            **kwargs,
        )
    )


def _contract(*, labels: tuple[str, ...] = ()) -> Contract:
    fields = {
        "name": "context test",
        "description": "",
        "inputs": [],
        "outputs": [],
        "activation": "",
        "budget": 2,
        "tool_scope": [],
        "labels": list(labels),
        "origin": "human",
    }
    return Contract(
        id=hash_contract(**fields), name=fields["name"], labels=labels, budget=2
    )


def _store(
    *assets: Asset, contract: Contract | None = None
) -> tuple[SQLiteStore, Contract]:
    store = SQLiteStore(":memory:")
    for asset in assets:
        store.add_asset(asset)
    selected = contract or _contract(labels=tuple(asset.name for asset in assets))
    store.add_contract(selected)
    return store, selected


def test_context_metadata_is_allowlisted_and_exact_asset_body_is_opt_in():
    marker = "PRIVATE_CONTEXT_MARKER"
    asset = _asset("guide", marker, source_uri="private://source")
    store, contract = _store(asset)

    metadata = task_context(store, contract.id)
    assert not contract.id.startswith(("task:v4:", "task:v5:", "task:v6:"))
    assert metadata["data"]["disclosed_assets"] == [
        {
            "id": asset.id,
            "name": "guide",
            "content_type": "text",
            "origin": "test",
            "trust_tier": "untrusted",
            "promptable": True,
            "disclosure_view": "original",
            "tombstoned": False,
        }
    ]
    assert marker not in json.dumps(metadata)
    assert "private://source" not in json.dumps(metadata)
    selected = task_context(store, contract.id, asset_id=asset.id, include_content=True)
    assert selected["data"]["selected_asset"]["content"] == marker


def test_context_requires_exact_disclosed_id_and_denies_nonpromptable_or_tombstone():
    visible = _asset("visible", "ok")
    hidden = _asset("hidden", "secret", promptable=False)
    tombstone = _asset("old", "old secret", tombstoned=True)
    store, contract = _store(visible, hidden, tombstone)
    with pytest.raises(TaskContextError, match="asset_not_disclosed"):
        task_context(store, contract.id, asset_id="by-name-visible")
    with pytest.raises(TaskContextError) as denied_hidden:
        task_context(store, contract.id, asset_id=hidden.id, include_content=True)
    assert denied_hidden.value.code == "asset_not_disclosed"
    with pytest.raises(TaskContextError) as denied_tombstone:
        task_context(store, contract.id, asset_id=tombstone.id, include_content=True)
    assert denied_tombstone.value.code == "asset_content_forbidden"


def test_nonoriginal_context_returns_existing_redacted_representation():
    asset = _asset("masked", "raw-secret", disclosure_view="summary")
    store, contract = _store(asset)
    result = task_context(store, contract.id, asset_id=asset.id, include_content=True)
    assert result["data"]["selected_asset"]["content"] == REDACTED_CONTENT


def test_frozen_label_context_does_not_widen_when_same_name_asset_is_added():
    original = _asset("guide", "frozen", trust_tier="configured")
    store = SQLiteStore(":memory:")
    store.add_asset(original)
    contract = bind_contract_label_assets(
        build_control_plane_contract(name="frozen task", labels=("guide",)), store
    )
    store.add_contract(contract)
    later = _asset("guide", "later", trust_tier="configured")
    store.add_asset(later)

    result = task_context(store, contract.id)
    assert [row["id"] for row in result["data"]["disclosed_assets"]] == [original.id]


def test_legacy_v3_label_context_retains_existing_name_resolution():
    original = _asset("guide", "first")
    later = _asset("guide", "second")
    store, contract = _store(original, later)

    result = task_context(store, contract.id)
    assert not contract.id.startswith(("task:v4:", "task:v5:", "task:v6:"))
    assert [row["id"] for row in result["data"]["disclosed_assets"]] == [
        original.id,
        later.id,
    ]


def test_v5_context_uses_frozen_asset_ids():
    asset = _asset("guide", "frozen", trust_tier="configured")
    store = SQLiteStore(":memory:")
    store.add_asset(asset)
    v4 = bind_contract_label_assets(
        build_control_plane_contract(name="v5 task", labels=("guide",)), store
    )
    fields = {
        "name": v4.name,
        "description": v4.description,
        "inputs": list(v4.inputs),
        "outputs": list(v4.outputs),
        "activation": v4.activation,
        "budget": v4.budget,
        "tool_scope": list(v4.tool_scope),
        "labels": list(v4.labels),
        "origin": v4.origin,
        "context_asset_ids": list(v4.context_asset_ids),
        "delegation_capabilities": [],
        "delegation_pools": [],
    }
    v5 = replace(v4, id=hash_contract_v5(**fields))
    store.add_contract(v5)
    store.add_asset(_asset("guide", "later", trust_tier="configured"))

    result = task_context(store, v5.id)
    assert v5.id.startswith("task:v5:")
    assert [row["id"] for row in result["data"]["disclosed_assets"]] == [asset.id]


def test_v6_mapped_input_uses_its_exact_asset_when_later_name_twin_exists():
    store = SQLiteStore(":memory:")
    setup = candidate_runtime(store)
    original = setup.accept_asset(_asset("dataset", "selected"))
    contract = contract_from_fields(
        name="mapped input",
        description="",
        inputs=("dataset",),
        outputs=(),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        sensitive_input_policy={
            "input_asset_bindings": {"dataset": [original.id]},
        },
        origin="human",
    )
    setup.accept_contract(contract)
    setup.accept_asset(_asset("dataset", "later twin"))

    result = task_context(store, contract.id)
    assert contract.id.startswith("task:v6:")
    assert [row["id"] for row in result["data"]["disclosed_assets"]] == [original.id]


def test_method_context_rows_keep_shared_selector_order_and_safe_body_fence():
    store = SQLiteStore(":memory:")
    setup = candidate_runtime(store)
    contract = setup.accept_contract(
        Contract(id="", name="method context", outputs=("result",))
    )
    first = setup.accept_asset(_asset("method-first", "first"))
    redacted = setup.accept_asset(
        _asset("method-redacted", "secret", disclosure_view="redacted")
    )
    hidden = setup.accept_asset(_asset("method-hidden", "hidden", promptable=False))
    store.append_trace_entry(
        create_entry(
            contract.id,
            "method_continuation_context_prepared",
            relation_target=contract.id,
            disclosed_assets=(first.id, "missing:asset", first.id, hidden.id),
        )
    )
    store.append_trace_entry(
        create_entry(
            contract.id,
            "method_continuation_scheduled",
            relation_target=contract.id,
            disclosed_assets=(redacted.id, first.id),
        )
    )

    result = task_context(store, contract.id)
    rows = result["data"]["method_context_assets"]
    assert [row["id"] for row in rows] == [first.id, hidden.id, redacted.id]
    assert next(row for row in rows if row["id"] == hidden.id)["promptable"] is False
    with pytest.raises(TaskContextError) as denied:
        task_context(store, contract.id, asset_id=hidden.id, include_content=True)
    assert denied.value.code == "asset_content_forbidden"
    redacted_body = task_context(
        store, contract.id, asset_id=redacted.id, include_content=True
    )
    assert redacted_body["data"]["selected_asset"]["content"] == REDACTED_CONTENT


def test_sensitive_policy_rejection_does_not_return_policy_reasons():
    store = SQLiteStore(":memory:")
    setup = candidate_runtime(store)
    setup.accept_asset(_asset("dataset", "source", origin="source-signer"))
    contract = contract_from_fields(
        name="sensitive input",
        description="",
        inputs=("dataset",),
        outputs=(),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        sensitive_input_policy={
            "required_signer": "a-different-signer",
            "required_trust_tier": "trusted",
        },
        origin="human",
    )
    setup.accept_contract(contract)
    with pytest.raises(TaskContextError) as rejected:
        task_context(store, contract.id)
    assert rejected.value.code == "disclosure_rejected"
    assert "a-different-signer" not in str(rejected.value)


@pytest.mark.parametrize(
    "missing", [True, False], ids=["missing-bound-asset", "wrong-role"]
)
def test_invalid_exact_input_bindings_fail_without_echoing_binding(missing):
    store = SQLiteStore(":memory:")
    if missing:
        bound_id = "private-missing-asset-id"
    else:
        wrong_role_asset = _asset("other-role", "private-marker")
        store.add_asset(wrong_role_asset)
        bound_id = wrong_role_asset.id
    contract = contract_from_fields(
        name="invalid mapping",
        description="",
        inputs=("dataset",),
        outputs=(),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        sensitive_input_policy={"input_asset_bindings": {"dataset": [bound_id]}},
        origin="human",
    )
    store.add_contract(contract)

    with pytest.raises(TaskContextError) as rejected:
        task_context(store, contract.id)
    assert rejected.value.code == "context_invalid"
    assert bound_id not in str(rejected.value)


def test_invalid_task_and_flag_combinations_have_safe_codes():
    store, contract = _store()
    with pytest.raises(TaskContextError) as unknown:
        task_context(store, "missing-task")
    assert unknown.value.code == "task_not_found"
    with pytest.raises(TaskContextError) as no_id:
        task_context(store, contract.id, include_content=True)
    assert no_id.value.code == "invalid_arguments"
    with pytest.raises(TaskContextError) as malformed_opt_in:
        task_context(store, contract.id, asset_id="asset", include_content="yes")
    assert malformed_opt_in.value.code == "invalid_arguments"


def test_cli_json_and_human_names_escape_control_characters(monkeypatch):
    asset = _asset("line\nInjected heading", "text")
    store, contract = _store(asset)
    monkeypatch.setattr(
        "aigineering.cli.task_context._persistent_snapshot", lambda: _store(asset)[0]
    )

    class Collaboration:
        @staticmethod
        def sync():
            return None

    monkeypatch.setattr(
        "aigineering.cli.task_context.collaboration_service",
        lambda _store: Collaboration(),
    )
    cli = Group()
    register_task_context_command(cli)
    runner = CliRunner()
    response = runner.invoke(cli, ["context", contract.id, "--json"])
    assert response.exit_code == 0
    assert (
        json.loads(response.output)["data"]["disclosed_assets"][0]["name"] == asset.name
    )
    human = runner.invoke(cli, ["context", contract.id])
    assert human.exit_code == 0
    assert "line\\nInjected heading" in human.output
    assert '"origin": "test"' in human.output
    assert '"trust_tier": "untrusted"' in human.output
    denied = runner.invoke(
        cli,
        ["context", contract.id, "--asset", "PRIVATE_UNDISCLOSED_ID", "--json"],
    )
    assert denied.exit_code == 1
    assert json.loads(denied.output) == {
        "ok": False,
        "error": "asset_not_disclosed",
    }
    assert "PRIVATE_UNDISCLOSED_ID" not in denied.output
    invalid = runner.invoke(
        cli, ["context", contract.id, "--include-content", "--json"]
    )
    assert invalid.exit_code == 1
    assert json.loads(invalid.output) == {"ok": False, "error": "invalid_arguments"}
