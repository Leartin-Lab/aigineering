"""HTTP Worker claims use the same exact prerequisite gate as local claims."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from aigineering.core.commitment import CandidateCommitter
from aigineering.core.domain import initialize_genesis
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.task_dependencies import TASK_DEPENDENCY_EFFECTS
from aigineering.protocol.candidate import (
    ActorKey,
    create_candidate_proposal,
    create_genesis_manifest,
    candidate_proposal_to_dict,
)
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    contract_declaration_effect,
    task_dependency_link_effect,
    worker_claim_effect,
    worker_registration_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.server.app import app


def _publish(store, genesis, signer, actor, effects, key, *, additional_effects=None):
    proposal = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=tuple(effects),
        signer=signer,
        idempotency_key=key,
    )
    return CandidateCommitter(
        store, store, additional_effects=additional_effects
    ).commit(proposal, genesis)


def test_http_worker_claim_waits_for_exact_prerequisite_terminal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("aigineering.server.app.sync_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr("aigineering.server.app.require_redis", lambda: None)
    store = SQLiteStore(".aig/store.db")
    signer = Ed25519Signer()
    operator = ActorKey(
        "human:dependency-operator",
        "operator-key",
        signer.kind,
        signer.signer_id,
        (
            "actor.authorize",
            "contract.publish",
            "task.dependency.manage",
            "worker.register",
        ),
    )
    genesis = create_genesis_manifest(
        "http-dependency-gate", (operator,), "policy:http-dependency-gate"
    )
    initialize_genesis(store, genesis)
    target = contract_from_fields(
        name="http-gated-target",
        description="must wait for the prerequisite",
        inputs=(),
        outputs=(),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        origin="human",
    )
    prerequisite = contract_from_fields(
        name="http-prerequisite",
        description="complete before claim",
        inputs=(),
        outputs=(),
        activation="",
        budget=1,
        tool_scope=(),
        labels=(),
        origin="human",
    )
    assert _publish(
        store,
        genesis,
        signer,
        operator,
        (
            contract_declaration_effect(target),
            contract_declaration_effect(prerequisite),
        ),
        "declare-http-dependency-contracts",
    ).accepted
    linked = _publish(
        store,
        genesis,
        signer,
        operator,
        (task_dependency_link_effect(target.id, prerequisite.id, "must complete"),),
        "link-http-dependency",
        additional_effects=TASK_DEPENDENCY_EFFECTS,
    )
    assert linked.accepted, linked

    worker_signer = Ed25519Signer()
    worker = ActorKey(
        "worker:http-dependency",
        "worker-key",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    registration = WorkerRegistration(
        worker.actor_id,
        actor_id=worker.actor_id,
        key_id=worker.key_id,
    )
    registered = _publish(
        store,
        genesis,
        signer,
        operator,
        (
            actor_authorization_effect(worker),
            worker_registration_effect(registration),
        ),
        "register-http-dependency-worker",
    )
    assert registered.accepted, registered
    store.close()

    client = TestClient(app)

    def claim(request_id: str):
        proposal = create_candidate_proposal(
            domain_id=genesis.id,
            actor_id=worker.actor_id,
            key_id=worker.key_id,
            effects=(worker_claim_effect(worker.actor_id, contract_id=target.id),),
            signer=worker_signer,
            idempotency_key=request_id,
        )
        return client.post("/worker/claims", json=candidate_proposal_to_dict(proposal))

    blocked = claim("http-blocked-claim")
    assert blocked.status_code == 409, blocked.text
    check = SQLiteStore.snapshot_from_path(".aig/store.db")
    assert check.get_claim(target.id) is None
    check.close()

    store = SQLiteStore(".aig/store.db")
    store.append_runtime_record(
        create_terminal_record(
            prerequisite.id,
            "complete",
            actor_id="test:fixture",
            reason="completed prerequisite",
        )
    )
    store.close()
    allowed = claim("http-allowed-claim")
    assert allowed.status_code == 200, allowed.text
    check = SQLiteStore.snapshot_from_path(".aig/store.db")
    assert check.get_claim(target.id) is not None
    check.close()
