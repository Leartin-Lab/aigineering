"""Tests for optional FastAPI server surface."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.asset_versions import create_slice_asset
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    candidate_proposal_to_dict,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.wire import contract_to_dict
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.server.app import app


def _signed_client():
    signer = Ed25519Signer()
    genesis = create_genesis_manifest(
        "server-test",
        [
            ActorKey(
                "test:client",
                "root",
                signer.kind,
                signer.signer_id,
                ("asset.publish", "contract.publish"),
            )
        ],
        "policy:test",
    )
    store = SQLiteStore(".aig/store.db")
    initialize_genesis(store, genesis)
    store.close()
    return TestClient(app), (signer, genesis)


def _candidate_body(actor, effect: CandidateEffect):
    signer, genesis = actor
    return candidate_proposal_to_dict(
        create_candidate_proposal(
            domain_id=genesis.id,
            actor_id="test:client",
            key_id="root",
            effects=[effect],
            signer=signer,
        )
    )


def _post_asset(client, actor, **fields):
    payload = {
        "content": fields.pop("content"),
        "name": fields.pop("name"),
        **fields,
    }
    return client.post(
        "/assets",
        json=_candidate_body(
            actor, CandidateEffect("asset.propose", {"asset": payload})
        ),
    )


def _post_contract(client, actor, **fields):
    contract = build_control_plane_contract(
        name=fields.pop("name"),
        inputs=tuple(fields.pop("inputs", ())),
        outputs=tuple(fields.pop("outputs", ())),
        labels=tuple(fields.pop("labels", ())),
        tool_scope=tuple(fields.pop("tool_scope", ())),
        **fields,
    )
    return client.post(
        "/contracts",
        json=_candidate_body(
            actor,
            CandidateEffect(
                "contract.declare", {"contract": contract_to_dict(contract)}
            ),
        ),
    )


def test_create_and_get_asset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, actor = _signed_client()

    created = _post_asset(
        client, actor, name="api_doc", content="hello", trust_tier="human"
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "api_doc"
    assert body["content"] == "hello"
    assert body["definition_hash"].startswith("def:")

    fetched = client.get("/assets/api_doc")
    assert fetched.status_code == 200, fetched.text
    rows = fetched.json()
    assert rows[0]["id"] == body["id"]


def test_create_protected_asset_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, actor = _signed_client()

    result = _post_asset(client, actor, name="_sys_secret", content="x")
    assert result.status_code == 422
    assert "protected" in result.json()["detail"].lower()


def test_unsigned_resource_write_is_rejected_without_mutation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, _ = _signed_client()

    result = client.post("/assets", json={"name": "unsigned", "content": "x"})

    assert result.status_code == 422
    store = SQLiteStore(".aig/store.db")
    assert store.get_all_assets() == []
    store.close()


def test_resource_endpoint_rejects_wrong_effect_before_commit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, actor = _signed_client()
    asset_candidate = _candidate_body(
        actor,
        CandidateEffect(
            "asset.propose", {"asset": {"name": "wrong-route", "content": "x"}}
        ),
    )

    result = client.post("/contracts", json=asset_candidate)

    assert result.status_code == 422
    store = SQLiteStore(".aig/store.db")
    assert store.get_assets_by_name("wrong-route") == []
    assert store.scan_runtime_records(record_type="candidate.received") == []
    store.close()


def test_generic_candidate_endpoint_returns_decision_evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, actor = _signed_client()
    body = _candidate_body(
        actor,
        CandidateEffect(
            "asset.propose", {"asset": {"name": "generic", "content": "value"}}
        ),
    )

    result = client.post("/candidates", json=body)

    assert result.status_code == 200, result.text
    decision = result.json()
    assert decision["accepted"] is True
    assert decision["candidate_id"] == body["id"]
    assert decision["assets"][0]["name"] == "generic"
    assert "candidate.received" in decision["record_types"]


def test_create_and_get_contract(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, actor = _signed_client()

    created = _post_contract(
        client,
        actor,
        name="api_task",
        inputs=["api_doc"],
        outputs=["out"],
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "api_task"
    assert body["outputs"] == ["out"]

    fetched = client.get(f"/contracts/{body['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == body["id"]


def test_list_assets_and_contracts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, actor = _signed_client()

    asset = _post_asset(
        client, actor, name="api_doc", content="hello", trust_tier="human"
    )
    assert asset.status_code == 201, asset.text
    contract = _post_contract(
        client,
        actor,
        name="api_task",
        inputs=["api_doc"],
        outputs=["out"],
    )
    assert contract.status_code == 201, contract.text

    assets = client.get("/assets")
    assert assets.status_code == 200, assets.text
    assert [row["name"] for row in assets.json()] == ["api_doc"]

    contracts = client.get("/contracts")
    assert contracts.status_code == 200, contracts.text
    assert [row["name"] for row in contracts.json()] == ["api_task"]


def test_run_contract_persists_outputs_and_trace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, actor = _signed_client()

    created = _post_contract(client, actor, name="api_task", outputs=["out"], budget=1)
    assert created.status_code == 201, created.text
    contract_id = created.json()["id"]

    run = client.post(
        f"/contracts/{contract_id}/run",
        json={"worker": "mock", "output_content": "done"},
    )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["contract_id"] == contract_id
    assert body["status"] == "complete"
    assert body["trace_ids"]
    assert len(body["output_asset_ids"]) == 1

    output = client.get("/assets/out")
    assert output.status_code == 200, output.text
    assert output.json()[0]["content"] == "done"

    trace = client.get("/trace")
    assert trace.status_code == 200, trace.text
    assert "complete" in [entry["event_type"] for entry in trace.json()]
    store = SQLiteStore(".aig/store.db")
    record_types = [record.record_type for _, record in store.scan_runtime_records()]
    assert "claim.granted" in record_types
    assert "candidate.received" in record_types
    assert "projection.decided" in record_types
    assert "claim.submitted" in record_types


def test_run_contract_rejects_non_mock_worker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, actor = _signed_client()

    created = _post_contract(client, actor, name="api_task", outputs=["out"], budget=1)
    assert created.status_code == 201, created.text

    run = client.post(
        f"/contracts/{created.json()['id']}/run",
        json={"worker": "llm"},
    )
    assert run.status_code == 400
    assert "mock worker" in run.json()["detail"]


def test_worker_protocol_cross_replica_claim_renew_submit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    replica_a, actor = _signed_client()
    replica_b = TestClient(app)
    replica_c = TestClient(app)
    created = _post_contract(
        replica_a, actor, name="replicated", outputs=["out"], budget=1
    )
    contract_id = created.json()["id"]

    claimed = replica_a.post(
        "/worker/claims",
        json={
            "worker_id": "remote-worker",
            "contract_id": contract_id,
            "lease_seconds": 30,
        },
    )
    assert claimed.status_code == 200, claimed.text
    package = claimed.json()
    renewed = replica_b.post(
        f"/worker/claims/{package['claim_id']}/renew",
        json={
            "worker_id": "remote-worker",
            "claim_epoch": package["claim_epoch"],
            "lease_seconds": 30,
        },
    )
    assert renewed.status_code == 200, renewed.text

    submission = {
        "contract_id": contract_id,
        "worker_id": "remote-worker",
        "raw_output": "out: accepted across replicas",
        "package_id": package["package_id"],
        "claim_id": package["claim_id"],
        "claim_epoch": package["claim_epoch"],
        "idempotency_key": f"remote-{package['package_id']}",
    }
    submitted = replica_c.post("/worker/submissions", json=submission)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["complete"] is True
    output = replica_a.get("/assets/out")
    assert output.json()[0]["content"] == "accepted across replicas"
    duplicate = replica_b.post("/worker/submissions", json=submission)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    changed = replica_a.post(
        "/worker/submissions",
        json={**submission, "raw_output": "out: changed replay"},
    )
    assert changed.status_code == 409

    records = SQLiteStore(".aig/store.db").scan_runtime_records()
    record_types = [record.record_type for _, record in records]
    assert record_types.count("claim.granted") == 1
    assert "claim.renewed" in record_types
    assert record_types.count("claim.submitted") == 1


def test_worker_protocol_method_submission_uses_same_fenced_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, actor = _signed_client()
    created = _post_contract(
        client, actor, name="remote_plan", outputs=["report"], budget=2
    )
    contract_id = created.json()["id"]
    claimed = client.post(
        "/worker/claims",
        json={"worker_id": "remote-planner", "contract_id": contract_id},
    )
    package = claimed.json()

    submission = {
        "contract_id": contract_id,
        "worker_id": "remote-planner",
        "raw_output": '/plan {"reason": "decompose remotely"}',
        "package_id": package["package_id"],
        "claim_id": package["claim_id"],
        "claim_epoch": package["claim_epoch"],
        "idempotency_key": f"remote-{package['package_id']}",
    }
    submitted = client.post("/worker/submissions", json=submission)

    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["status"] == "method_scheduled"
    child = SQLiteStore(".aig/store.db").get_contract(body["child_contract_id"])
    assert child is not None
    assert "method:plan" in child.labels
    duplicate = client.post("/worker/submissions", json=submission)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    changed = client.post(
        "/worker/submissions",
        json={**submission, "raw_output": '/plan {"reason": "changed"}'},
    )
    assert changed.status_code == 409


def test_asset_slice_versions_and_replacement_claims(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, actor = _signed_client()

    created = _post_asset(
        client,
        actor,
        name="doc",
        content="line one\nline two\nline three\n",
        trust_tier="human",
    )
    assert created.status_code == 201, created.text
    source = created.json()

    unsigned = client.post(
        "/assets/doc/slice",
        json={"slice_name": "doc_excerpt", "range": "lines:2-3"},
    )
    assert unsigned.status_code == 422

    mismatched = _candidate_body(
        actor,
        CandidateEffect(
            "asset.propose",
            {"asset": {"name": "doc_excerpt", "content": "forged"}},
        ),
    )
    mismatched["range"] = "lines:2-3"
    mismatch_response = client.post("/assets/doc/slice", json=mismatched)
    assert mismatch_response.status_code == 422

    store = SQLiteStore(".aig/store.db")
    source_asset = store.get_asset(source["id"])
    assert source_asset is not None
    proposed_slice = create_slice_asset(
        source_asset,
        slice_name="doc_excerpt",
        range_spec="lines:2-3",
    )
    store.close()
    slice_candidate = _candidate_body(actor, asset_proposal_effect(proposed_slice))
    slice_candidate["range"] = "lines:2-3"
    sliced = client.post("/assets/doc/slice", json=slice_candidate)
    assert sliced.status_code == 201, sliced.text
    replacement = sliced.json()
    assert replacement["name"] == "doc_excerpt"
    assert replacement["content"] == "line two\nline three\n"

    versions = client.get("/assets/doc/versions")
    assert versions.status_code == 200, versions.text
    assert [row["id"] for row in versions.json()] == [source["id"]]

    claim = client.post(
        "/replacement-claims",
        json={
            "source_asset_id": source["id"],
            "replacement_asset_id": replacement["id"],
            "claim_type": "summary",
            "signed_by": "reviewer",
        },
    )
    assert claim.status_code == 201, claim.text
    claim_body = claim.json()
    assert claim_body["definition_hash"] == source["definition_hash"]
    assert claim_body["claim_type"] == "summary"

    by_source = client.get(
        "/replacement-claims",
        params={"source_asset_id": source["id"]},
    )
    assert by_source.status_code == 200, by_source.text
    assert [row["id"] for row in by_source.json()] == [claim_body["id"]]

    by_definition = client.get(
        "/replacement-claims",
        params={"definition_hash": source["definition_hash"]},
    )
    assert by_definition.status_code == 200, by_definition.text
    assert [row["id"] for row in by_definition.json()] == [claim_body["id"]]


def test_replacement_claim_rejects_invalid_claim_type(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, actor = _signed_client()

    first = _post_asset(client, actor, name="first", content="a", trust_tier="human")
    second = _post_asset(client, actor, name="second", content="b", trust_tier="human")
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    claim = client.post(
        "/replacement-claims",
        json={
            "source_asset_id": first.json()["id"],
            "replacement_asset_id": second.json()["id"],
            "claim_type": "teleport",
        },
    )
    assert claim.status_code == 400
    assert "Invalid claim_type" in claim.json()["detail"]
