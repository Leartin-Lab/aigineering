from __future__ import annotations

import pytest

from aigineering.core.disclosure import compute_disclosure
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_fleet import FleetRunResult
from aigineering.plugins.task_semantics import contracts_from_plan_asset
from aigineering.protocol.types import Asset
from aigineering.run_config import prepare_configured_run
from aigineering.configured_run import run_configured
from aigineering.configured_run import _published_input_asset_bindings


class _UnusedWorker:
    worker_id = "configured-input-binding-worker"

    def registration(self):
        from aigineering.core.worker_routing import WorkerRegistration

        return WorkerRegistration(self.worker_id)


def _write_preset(tmp_path, *, root_name, source_path, policy=""):
    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path=".aig/store.db"\npoll_interval=0.01\n'
        '[[workers]]\nid="configured-input-binding-worker"\nkind="local"\n'
        'worker_factory="fixture:worker"\n',
        encoding="utf-8",
    )
    preset = tmp_path / f"{root_name}.toml"
    preset.write_text(
        '[run]\nfleet="workers.toml"\n'
        f'[[inputs]]\nname="source"\npath="{source_path}"\n'
        f'[task]\nname="{root_name}"\ninputs=["source"]\n'
        f'activation="source"\noutputs=["{root_name}-result"]\n' + policy,
        encoding="utf-8",
    )
    return preset


def _install_local_fleet_stubs(monkeypatch, tmp_path):
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(tmp_path / "identity/root.ed25519"))
    monkeypatch.setattr("aigineering.configured_run.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.configured_run.sync_runtime", lambda _store: None)
    monkeypatch.setattr(
        "aigineering.fleet_config.build_fleet_worker",
        lambda _spec, **_kwargs: _UnusedWorker(),
    )

    def finish(_db_path, _hosts, *, target_contract_id, **_kwargs):
        return FleetRunResult(target_contract_id, "complete", True, False)

    monkeypatch.setattr("aigineering.configured_run.run_local_fleet", finish)


def test_configured_new_root_pins_current_inputs_and_resume_keeps_original_binding(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _install_local_fleet_stubs(monkeypatch, tmp_path)
    source = tmp_path / "source.json"
    source.write_text('{"value":"historical"}', encoding="utf-8")
    old_preset = _write_preset(
        tmp_path, root_name="historical-root", source_path="source.json"
    )
    old_result = run_configured(prepare_configured_run(old_preset), timeout=1)

    source.write_text('{"value":"current"}', encoding="utf-8")
    policy = (
        "[task.sensitive_input_policy]\n"
        'minimum_trust_tier="untrusted"\n'
        'reserved_prefixes=["_private_"]\n'
        'input_asset_bindings={unused=["asset:legacy"]}\n'
    )
    current_preset = _write_preset(
        tmp_path,
        root_name="current-root",
        source_path="source.json",
        policy=policy,
    )
    current_result = run_configured(prepare_configured_run(current_preset), timeout=1)

    store = SQLiteStore(tmp_path / ".aig/store.db")
    try:
        current = store.get_contract(current_result.contract_id)
        assert current is not None
        assert current.id.startswith("task:v6:")
        assets = store.get_assets_by_name("source")
        by_content = {asset.content: asset for asset in assets}
        selected = by_content['{"value":"current"}']
        historical = by_content['{"value":"historical"}']
        assert current.sensitive_input_policy["minimum_trust_tier"] == "untrusted"
        assert current.sensitive_input_policy["reserved_prefixes"] == ("_private_",)
        assert current.sensitive_input_policy["input_asset_bindings"] == {
            "source": (selected.id,),
            "unused": ("asset:legacy",),
        }
        disclosed_ids = {asset.id for asset in compute_disclosure(current, store)}
        assert disclosed_ids == {selected.id}
        assert historical.id not in disclosed_ids
        plan = Asset(
            id="uncommitted-plan-fixture",
            name=f"_plan_result_{current.id}",
            content=(
                '{"contracts":[{"name":"child","description":"Use source.",'
                '"inputs":["source"],"outputs":["current-root-result"],'
                '"activation":"source","budget":1,"tool_scope":[],"labels":[]}]}'
            ),
        )
        children, rejected = contracts_from_plan_asset(
            plan,
            current.id,
            parent_contract=current,
            allowed_input_names={"source"},
        )
        assert rejected == []
        assert len(children) == 1
        assert children[0].id.startswith("task:v6:")
        assert children[0].sensitive_input_policy["input_asset_bindings"] == {
            "source": (selected.id,),
            "unused": ("asset:legacy",),
        }
    finally:
        store.close()

    source.unlink()
    resume_prepared = prepare_configured_run(
        current_preset, resume_task_id=current_result.contract_id
    )
    resumed = run_configured(resume_prepared, timeout=1)
    assert resumed.contract_id == current_result.contract_id
    store = SQLiteStore(tmp_path / ".aig/store.db")
    try:
        current = store.get_contract(current_result.contract_id)
        assert current is not None
        assert current.sensitive_input_policy["input_asset_bindings"] == {
            "source": (selected.id,),
            "unused": ("asset:legacy",),
        }
        assert [asset.id for asset in compute_disclosure(current, store)] == [
            selected.id
        ]
    finally:
        store.close()
    assert old_result.contract_id != current_result.contract_id


def test_configured_new_root_rejects_conflicting_active_binding_before_declaration(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _install_local_fleet_stubs(monkeypatch, tmp_path)
    source = tmp_path / "source.json"
    source.write_text('{"value":"current"}', encoding="utf-8")
    policy = (
        '[task.sensitive_input_policy]\ninput_asset_bindings={source=["asset:other"]}\n'
    )
    preset = _write_preset(
        tmp_path,
        root_name="conflicting-root",
        source_path="source.json",
        policy=policy,
    )

    with pytest.raises(ValueError, match="input_asset_bindings.*conflicts"):
        run_configured(prepare_configured_run(preset), timeout=1)

    store = SQLiteStore(tmp_path / ".aig/store.db")
    try:
        assert not store.get_all_contracts()
        assert [asset.name for asset in store.get_all_assets()] == ["source"]
    finally:
        store.close()


def test_configured_input_missing_from_publication_result_fails_closed():
    with pytest.raises(
        ValueError, match="did not resolve to exactly one published Asset"
    ):
        _published_input_asset_bindings(("source",), ())


def test_configured_root_without_sensitive_policy_pins_input_and_excludes_history(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _install_local_fleet_stubs(monkeypatch, tmp_path)
    source = tmp_path / "source.json"
    source.write_text('{"value":"historical"}', encoding="utf-8")
    prior_preset = _write_preset(
        tmp_path, root_name="prior-root", source_path="source.json"
    )
    prior = run_configured(prepare_configured_run(prior_preset), timeout=1)
    assert prior.completed

    source.write_text('{"value":"current"}', encoding="utf-8")
    preset = _write_preset(tmp_path, root_name="legacy-root", source_path="source.json")

    result = run_configured(prepare_configured_run(preset), timeout=1)

    store = SQLiteStore(tmp_path / ".aig/store.db")
    try:
        root = store.get_contract(result.contract_id)
        assert root is not None
        assert root.id.startswith("task:v6:")
        assets = store.get_assets_by_name("source")
        current = next(
            asset for asset in assets if asset.content == '{"value":"current"}'
        )
        historical = next(
            asset for asset in assets if asset.content == '{"value":"historical"}'
        )
        assert root.sensitive_input_policy["input_asset_bindings"] == {
            "source": (current.id,)
        }
        assert [asset.id for asset in compute_disclosure(root, store)] == [current.id]
        assert historical.id != current.id
    finally:
        store.close()
