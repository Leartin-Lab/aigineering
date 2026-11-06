from __future__ import annotations

import json

import pytest

from aigineering.core.worker_routing import WorkerRegistration
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Candidate
from aigineering.run_config import prepare_configured_run
from aigineering.configured_run import run_configured


class _WritingWorker:
    worker_id = "configured-test-worker"

    def registration(self):
        return WorkerRegistration(self.worker_id)

    def invoke(self, contract, disclosed_assets):
        del disclosed_assets
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps({"outputs": {name: "accepted" for name in contract.outputs}}),
        )


def test_configured_run_publishes_signed_inputs_root_and_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / "identity" / "root.ed25519")
    )
    monkeypatch.setattr(
        "aigineering.fleet_config.build_fleet_worker",
        lambda spec, **_kwargs: _WritingWorker(),
    )
    from aigineering.local_fleet import FleetRunResult

    def inspect_published_root(
        db_path, hosts, *, target_contract_id, timeout, poll_interval
    ):
        del hosts, timeout, poll_interval
        store = SQLiteStore(db_path)
        try:
            root = store.get_contract(target_contract_id)
            assert root is not None
            source = store.get_assets_by_name("source")
            skill = store.get_assets_by_name("_skill_content_demo")
            assert root.context_asset_ids == (skill[0].id,)
            assert root.acceptance_policy["evidence_asset_ids"] == (source[0].id,)
        finally:
            store.close()
        return FleetRunResult(target_contract_id, "complete", True, False)

    monkeypatch.setattr(
        "aigineering.configured_run.run_local_fleet", inspect_published_root
    )
    (tmp_path / "input.txt").write_text("source evidence", encoding="utf-8")
    (tmp_path / "skill").mkdir()
    (tmp_path / "skill" / "skill.toml").write_text(
        'name="demo"\nversion="1"\n', encoding="utf-8"
    )
    (tmp_path / "skill" / "skill.md").write_text(
        "Use the demo skill.", encoding="utf-8"
    )
    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path=".aig/store.db"\npoll_interval=0.02\n'
        '[[workers]]\nid="configured-test-worker"\nkind="local"\nworker_factory="example:factory"\n',
        encoding="utf-8",
    )
    preset = tmp_path / "run.toml"
    preset.write_text(
        '[run]\nfleet="workers.toml"\nskills=["skill"]\n'
        '[[inputs]]\nname="source"\npath="input.txt"\n'
        '[task]\nname="root"\ninputs=["source"]\nactivation="source"\noutputs=["result"]\n'
        'labels=["_skill_content_demo"]\nacceptance_evidence_inputs=["source"]\n'
        '[task.acceptance_policy]\nmode="independent"\npolicy_version="test-v1"\n',
        encoding="utf-8",
    )

    prepared = prepare_configured_run(preset)
    result = run_configured(prepared, timeout=3)

    assert result.completed
    assert prepared.contract is not None
    store = SQLiteStore(".aig/store.db")
    try:
        root = store.get_contract(result.contract_id)
        assert root is not None
        source = store.get_assets_by_name("source")
        skill = store.get_assets_by_name("_skill_content_demo")
        assert source and skill
        assert root.context_asset_ids == (skill[0].id,)
        assert root.acceptance_policy["evidence_asset_ids"] == (source[0].id,)
        assert not store.get_assets_by_name("result")
    finally:
        store.close()

    (tmp_path / "input.txt").unlink()
    import shutil

    shutil.rmtree(tmp_path / "skill")
    resume_prepared = prepare_configured_run(preset, resume_task_id=result.contract_id)
    resumed = run_configured(resume_prepared, timeout=1)
    assert resumed.contract_id == result.contract_id
    assert resumed.completed

    with pytest.raises(ValueError, match="resume it"):
        run_configured(prepared, timeout=1)


def test_configured_run_publishes_relative_file_registry_outside_workspace(
    tmp_path, monkeypatch
):
    import sys
    from types import ModuleType

    from aigineering.agent.tool_worker import ToolWorker
    from aigineering.local_fleet import FleetRunResult

    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "configs"
    workspace.mkdir()
    config_dir.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(workspace / "identity" / "root.ed25519")
    )
    monkeypatch.setattr("aigineering.configured_run.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.application.require_redis", lambda *_a, **_k: None)
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.runtime_services.sync_runtime", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "aigineering.configured_run.sync_runtime", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_k: None
    )

    module = ModuleType("configured_run_test_worker")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(
        "aigineering.fleet_config.build_fleet_worker",
        lambda spec, **kwargs: (
            _WritingWorker()
            if spec.kind == "local"
            else ToolWorker(kwargs["registry"], worker_id=spec.worker_id)
        ),
    )
    registry_path = config_dir / "registry.py"
    registry_path.write_text(
        "from aigineering.core.tools import ToolRegistry\n"
        "from aigineering.protocol.types import ToolSpec\n"
        "def build_registry():\n"
        "    registry = ToolRegistry()\n"
        "    registry.register(ToolSpec(name='configured_lookup', version='1.0'), lambda _args: 'ok')\n"
        "    return registry\n",
        encoding="utf-8",
    )
    (config_dir / "input.txt").write_text("configured input", encoding="utf-8")
    (config_dir / "workers.toml").write_text(
        '[fleet]\ndb_path=".aig/store.db"\n'
        '[[workers]]\nid="configured-test-worker"\nkind="local"\n'
        'worker_factory="configured_run_test_worker:build_worker"\n'
        '[[workers]]\nid="configured-tool-worker"\nkind="tool"\n'
        'tool_registry="./registry.py:build_registry"\n',
        encoding="utf-8",
    )
    run_config = config_dir / "run.toml"
    run_config.write_text(
        '[run]\nfleet="workers.toml"\n'
        '[[inputs]]\nname="source"\npath="input.txt"\n'
        '[task]\nname="configured-root"\ninputs=["source"]\n'
        'activation="source"\noutputs=["result"]\n',
        encoding="utf-8",
    )

    def inspect_published_registry(db_path, hosts, **kwargs):
        del hosts, kwargs
        store = SQLiteStore(db_path)
        try:
            descriptors = store.get_assets_by_name("_tool_capability_configured_lookup")
            assert len(descriptors) == 1
        finally:
            store.close()
        return FleetRunResult("configured-root", "complete", True, False)

    monkeypatch.setattr(
        "aigineering.configured_run.run_local_fleet", inspect_published_registry
    )
    prepared = prepare_configured_run(run_config)
    assert prepared.tool_registries[0][0] == f"{registry_path.resolve()}:build_registry"
    result = run_configured(prepared, timeout=2)
    assert result.completed


def test_response_format_reaches_fleet_llm_worker(tmp_path):
    from aigineering.fleet_config import build_fleet_worker

    config = tmp_path / "workers.toml"
    config.write_text(
        '[fleet]\ndb_path="store.db"\n[[workers]]\nid="json-worker"\nkind="llm"\nmodel="unused"\nresponse_format="json_object"\n',
        encoding="utf-8",
    )
    spec = prepare_configured_run(config, resume_task_id="existing").fleet.workers[0]
    worker = build_fleet_worker(spec)
    assert worker._response_format == "json_object"


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_invalid_timeout_fails_before_store_or_identity(tmp_path, monkeypatch, timeout):
    monkeypatch.chdir(tmp_path)
    key_path = tmp_path / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    monkeypatch.setattr(
        "aigineering.fleet_config.build_fleet_worker",
        lambda spec, **_kwargs: _WritingWorker(),
    )
    (tmp_path / "input.txt").write_text("source", encoding="utf-8")
    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path=".aig/store.db"\n[[workers]]\nid="configured-test-worker"\nkind="local"\nworker_factory="unused:factory"\n',
        encoding="utf-8",
    )
    (tmp_path / "run.toml").write_text(
        '[run]\nfleet="workers.toml"\n[[inputs]]\nname="source"\npath="input.txt"\n'
        '[task]\nname="root"\ninputs=["source"]\noutputs=["result"]\n',
        encoding="utf-8",
    )
    prepared = prepare_configured_run(tmp_path / "run.toml")
    with pytest.raises(ValueError, match="timeout"):
        run_configured(prepared, timeout=timeout)
    assert not (tmp_path / ".aig").exists()
    assert not key_path.exists()


def test_resume_requires_existing_store_before_creating_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / "identity" / "root.ed25519")
    )
    monkeypatch.setattr(
        "aigineering.fleet_config.build_fleet_worker",
        lambda spec, **_kwargs: _WritingWorker(),
    )
    workers = tmp_path / "workers.toml"
    workers.write_text(
        '[fleet]\ndb_path=".aig/store.db"\n[[workers]]\nid="configured-test-worker"\nkind="local"\nworker_factory="unused:factory"\n',
        encoding="utf-8",
    )
    prepared = prepare_configured_run(workers, resume_task_id="missing-root")
    with pytest.raises(ValueError, match="Store does not exist"):
        run_configured(prepared)
    assert not (tmp_path / ".aig").exists()
    assert not (tmp_path / "identity" / "root.ed25519").exists()


def test_unrelated_historical_output_refuses_new_setup_without_publication(
    tmp_path, monkeypatch
):
    from aigineering.core.control_plane import build_control_plane_asset
    from aigineering.local_identity import ensure_local_runtime_publishers
    from aigineering.local_publication import commit_local_effect, require_accepted
    from aigineering.protocol.effect_builders import asset_proposal_effect

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(tmp_path / "root.ed25519"))
    monkeypatch.setattr(
        "aigineering.fleet_config.build_fleet_worker", lambda spec: _WritingWorker()
    )
    (tmp_path / "input.txt").write_text("new source", encoding="utf-8")
    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path=".aig/store.db"\n'
        '[[workers]]\nid="configured-test-worker"\nkind="local"\n'
        'worker_factory="unused:factory"\n',
        encoding="utf-8",
    )
    (tmp_path / "run.toml").write_text(
        '[run]\nfleet="workers.toml"\n'
        '[[inputs]]\nname="source"\npath="input.txt"\n'
        '[task]\nname="new-root"\ninputs=["source"]\noutputs=["result"]\n',
        encoding="utf-8",
    )
    store = SQLiteStore(".aig/store.db")
    try:
        ensure_local_runtime_publishers(store)
        require_accepted(
            commit_local_effect(
                store,
                asset_proposal_effect(
                    build_control_plane_asset(
                        name="result", content="unrelated old result"
                    )
                ),
                idempotency_key="historical-result",
            )
        )
        records_before = store.get_runtime_revision()
    finally:
        store.close()
    prepared = prepare_configured_run(tmp_path / "run.toml")
    with pytest.raises(ValueError, match="fresh workspace"):
        run_configured(prepared, timeout=1)
    store = SQLiteStore(".aig/store.db")
    try:
        assert store.get_runtime_revision() == records_before
        assert not store.get_assets_by_name("source")
        assert not store.get_all_contracts()
    finally:
        store.close()
