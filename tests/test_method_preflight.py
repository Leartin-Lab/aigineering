"""Preflight and batching guarantees for method test preparation."""

from __future__ import annotations

import json

import pytest

from aigineering.cli._candidate import commit_local_effects
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain
from aigineering.methods.service import MethodService


@pytest.fixture
def method_domain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SQLiteStore(str(tmp_path / "methods.db"))
    ensure_local_domain(store)
    batches = []

    def publish(effects, **kw):
        batches.append(tuple(effects))
        return commit_local_effects(store, effects, **kw)

    service = MethodService(store, publish)
    yield store, service, batches
    store.close()


def _package(*, tool=False, inputs=("source",)):
    return {
        "schema": "method-package-v1",
        "name": "preflight-check",
        "version": "1.0.0",
        "description": "Check inputs.",
        "instructions": "Check inputs.",
        "inputs": list(inputs),
        "outputs": {"assessment": {"matched": "boolean"}},
        "requirements": {
            "tool_scope": ["net"] if tool else [],
            "worker_capabilities": [],
            "worker_pools": [],
            "delegation_capabilities": [],
            "delegation_pools": [],
        },
        "dependencies": [],
        "context_asset_ids": [],
        "examples": [],
    }


def _suite(inputs=("source",)):
    return {
        "schema": "method-cases-v1",
        "name": "preflight-cases",
        "cases": [
            {
                "name": "one",
                "inputs": {slot: slot for slot in inputs},
                "expected": {"assessment": {"matched": True}},
            }
        ],
    }


def _assets_before(service):
    return {(asset.id, asset.name) for asset in service.store.get_all_assets()}


def test_failed_preflight_does_not_publish_inputs(method_domain):
    store, service, _ = method_domain
    method = service.import_package(json.dumps(_package(tool=True)))
    suite = service.import_cases(json.dumps(_suite()), "suite")
    before = _assets_before(service)

    with pytest.raises(ValueError, match="outside the explicit invocation grant"):
        service.prepare_tests(method.id, suite.id, name="tool-fail", budget=1)

    assert _assets_before(service) == before


def test_invalid_name_does_not_publish_inputs(method_domain):
    store, service, _ = method_domain
    method = service.import_package(json.dumps(_package()))
    suite = service.import_cases(json.dumps(_suite()), "suite")
    before = _assets_before(service)

    with pytest.raises(ValueError, match="ordinary identifier"):
        service.prepare_tests(method.id, suite.id, name="bad name", budget=1)

    assert _assets_before(service) == before


def test_multiple_case_inputs_are_published_and_bound(method_domain):
    store, service, batches = method_domain
    method = service.import_package(json.dumps(_package(inputs=("source", "extra"))))
    suite = service.import_cases(
        json.dumps(_suite(inputs=("source", "extra"))), "suite"
    )

    run, root = service.prepare_tests(method.id, suite.id, name="two-inputs", budget=1)

    assert run.id
    assert root.id
    names = {asset.name for asset in store.get_all_assets()}
    assert {"two-inputs.input0.source", "two-inputs.input0.extra"} <= names
    manifest = json.loads(run.content)
    bindings = manifest["cases"][0]["inputs"]
    assert set(bindings) == {"source", "extra"}
    child = store.get_contract(manifest["cases"][0]["contract_id"])
    assert child is not None
    assert set(bindings.values()) <= set(child.context_asset_ids)
    input_batches = [
        batch
        for batch in batches
        if sum(effect.effect_type == "asset.propose" for effect in batch) == 2
    ]
    assert len(input_batches) == 1


def test_zero_input_suite_does_not_publish_empty_candidate(method_domain):
    store, service, batches = method_domain
    method = service.import_package(json.dumps(_package(inputs=())))
    suite = service.import_cases(json.dumps(_suite(inputs=())), "suite")
    before = len(store.get_all_assets())

    run, root = service.prepare_tests(method.id, suite.id, name="no-inputs", budget=1)

    assert run.id and root.id
    assert len(store.get_all_assets()) == before + 1
    assert all(batch for batch in batches)


def test_repeated_failed_preflight_adds_no_facts_or_contracts(method_domain):
    store, service, _ = method_domain
    method = service.import_package(json.dumps(_package(tool=True)))
    suite = service.import_cases(json.dumps(_suite()), "suite")
    before_assets = len(store.get_all_assets())
    before_contracts = len(store.get_all_contracts())

    for _ in range(2):
        with pytest.raises(ValueError, match="outside the explicit invocation grant"):
            service.prepare_tests(method.id, suite.id, name="repeat-fail", budget=1)

    assert len(store.get_all_assets()) == before_assets
    assert len(store.get_all_contracts()) == before_contracts
