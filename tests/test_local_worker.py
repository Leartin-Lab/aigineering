from types import ModuleType, SimpleNamespace
import sys

import pytest

from aigineering.agent.local_worker import build_local_worker
from aigineering.fleet_config import FleetWorkerSpec, load_fleet_config
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain, ensure_local_worker_host
from aigineering.protocol.types import Candidate


class _Worker:
    def __init__(self, worker_id):
        self.worker_id = worker_id

    def invoke(self, contract, disclosed_assets):
        return Candidate(worker_id=self.worker_id, raw_output="/fail local-test")


def _spec(**kwargs):
    return FleetWorkerSpec(
        worker_id="local-1",
        kind="local",
        worker_factory=kwargs.pop("worker_factory", "m:factory"),
        **kwargs,
    )


def test_local_factory_is_loaded_only_when_building(monkeypatch):
    module = ModuleType("m")
    seen = []

    def factory(*, worker_id):
        seen.append(worker_id)
        return _Worker(worker_id)

    module.factory = factory
    monkeypatch.setitem(sys.modules, "m", module)
    worker = build_local_worker("m:factory", _spec(capabilities=("review",)))
    assert seen == ["local-1"]
    assert worker.registration().capabilities == ("review",)
    assert worker.registration().pools == ()


def test_local_factory_result_must_match_identity_and_invoke(monkeypatch):
    module = ModuleType("bad_local")
    module.factory = lambda **_: SimpleNamespace(
        worker_id="other", invoke=lambda *_: None
    )
    monkeypatch.setitem(sys.modules, "bad_local", module)
    with pytest.raises(ValueError, match="different worker_id"):
        build_local_worker("bad_local:factory", _spec())

    module.factory = lambda **_: SimpleNamespace(worker_id="local-1")
    with pytest.raises(ValueError, match="invoke-capable"):
        build_local_worker("bad_local:factory", _spec())


def test_local_factory_path_and_kind_validation(tmp_path):
    config = tmp_path / "fleet.toml"
    config.write_text(
        '[fleet]\n\n[[workers]]\nid="x"\nkind="llm"\nmodel="m"\nworker_factory="m:f"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="only valid for local"):
        load_fleet_config(config)

    config.write_text(
        '[fleet]\n\n[[workers]]\nid="x"\nkind="local"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="requires worker_factory"):
        load_fleet_config(config)

    config.write_text(
        '[fleet]\n\n[[workers]]\nid="x"\nkind="local"\nworker_factory="m:f"\ntool_registry="m:r"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot use tool_registry"):
        load_fleet_config(config)


def test_local_factory_import_is_deferred_until_build(tmp_path, monkeypatch):
    config = tmp_path / "fleet.toml"
    config.write_text(
        '[fleet]\n\n[[workers]]\nid="x"\nkind="local"\nworker_factory="missing_module:f"\n',
        encoding="utf-8",
    )
    spec = load_fleet_config(config).workers[0]
    assert spec.worker_factory == "missing_module:f"
    with pytest.raises(ValueError, match="cannot import"):
        build_local_worker(spec.worker_factory, spec)


def test_local_worker_uses_canonical_worker_host(tmp_path, monkeypatch):
    module = ModuleType("host_local")
    module.factory = lambda *, worker_id: _Worker(worker_id)
    monkeypatch.setitem(sys.modules, "host_local", module)
    spec = _spec(worker_factory="host_local:factory")
    worker = build_local_worker(spec.worker_factory, spec)
    store = SQLiteStore(str(tmp_path / "store.db"))
    try:
        ensure_local_domain(store)
        host = ensure_local_worker_host(store, worker)
        assert host.worker_id == "local-1"
        assert host.worker.invoke(SimpleNamespace(id="task"), []).worker_id == "local-1"
    finally:
        store.close()
