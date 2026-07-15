"""Conformance tests for the first Candidate commitment vertical slice."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from aigineering.core.commitment import CandidateCommitter, reduce_candidate
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.signing import Ed25519Signer, Signer, Verifier
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import actor_authorization_effect
from aigineering.protocol.types import Contract
from aigineering.protocol.wire import contract_to_dict


class _Signer(Signer):
    kind = "architecture-test"

    @property
    def signer_id(self) -> str:
        return "architecture-public-key"

    def sign(self, data: bytes) -> str:
        return hashlib.sha256(self.signer_id.encode() + data).hexdigest()


class _Verifier(Verifier):
    def verify(self, data: bytes, signature: str, signer_id: str) -> bool:
        return signature == hashlib.sha256(signer_id.encode() + data).hexdigest()


def _verifier_factory(kind: str) -> Verifier:
    assert kind == _Signer.kind
    return _Verifier()


def _contract() -> Contract:
    fields = {
        "name": "publish_report",
        "description": "Publish a reviewed report",
        "inputs": (),
        "outputs": ("report",),
        "activation": "",
        "budget": 5,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    return Contract(id=hash_contract_v3(**fields), **fields)


def _proposal(*, capabilities=("asset.publish", "contract.publish"), effect=None):
    signer = _Signer()
    genesis = create_genesis_manifest(
        "commitment-test",
        [ActorKey("human:owner", "root", signer.kind, signer.signer_id, capabilities)],
        "policy:test",
    )
    selected = effect or CandidateEffect(
        "contract.declare", {"contract": contract_to_dict(_contract())}
    )
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id="human:owner",
        key_id="root",
        effects=[selected],
        signer=signer,
    )
    return genesis, candidate


def test_reducer_is_pure_and_accepts_authorized_contract():
    genesis, candidate = _proposal()

    first = reduce_candidate(candidate, genesis, verifier_factory=_verifier_factory)
    second = reduce_candidate(candidate, genesis, verifier_factory=_verifier_factory)

    assert first.accepted is True
    assert first.contract == _contract()
    assert [record.id for record in first.runtime_records] == [
        record.id for record in second.runtime_records
    ]
    assert {record.record_type for record in first.runtime_records} >= {
        "candidate.received",
        "candidate.committed",
        "contract.declared",
    }


def test_worker_registration_candidate_updates_rebuildable_routing_view(store):
    registration = WorkerRegistration(
        "llm:vision",
        capabilities=("text", "vision"),
        pools=("advanced",),
        profile_id="deepseek:v4",
        capacity=2,
        version="3",
    )
    genesis, candidate = _proposal(
        capabilities=("worker.register",),
        effect=CandidateEffect(
            "worker.register",
            {
                "registration": {
                    "worker_id": registration.worker_id,
                    "capabilities": list(registration.capabilities),
                    "pools": list(registration.pools),
                    "profile_id": registration.profile_id,
                    "capacity": registration.capacity,
                    "enabled": registration.enabled,
                    "version": registration.version,
                }
            },
        ),
    )

    trace = store if isinstance(store, SQLiteStore) else MemoryTraceStore()
    decision = CandidateCommitter(store, trace).commit(
        candidate, genesis, verifier_factory=_verifier_factory
    )

    assert decision.accepted is True
    assert any(
        record.record_type == "worker.registered"
        and record.payload["worker_id"] == registration.worker_id
        for record in decision.runtime_records
    )
    assert store.get_worker_registration(registration.worker_id) == registration
    store.rebuild_worker_registration_projection()
    assert store.get_worker_registration(registration.worker_id) == registration


def test_worker_registration_requires_dedicated_actor_capability():
    genesis, candidate = _proposal(
        capabilities=("contract.publish",),
        effect=CandidateEffect(
            "worker.register", {"registration": {"worker_id": "unauthorized"}}
        ),
    )

    decision = reduce_candidate(candidate, genesis, verifier_factory=_verifier_factory)

    assert decision.accepted is False
    assert "worker.register" in str(decision.runtime_records[1].payload["reason"])


def test_authorized_actor_key_can_publish_its_own_candidate(store):
    root_signer = Ed25519Signer()
    worker_signer = Ed25519Signer()
    genesis = create_genesis_manifest(
        "delegated-worker",
        [
            ActorKey(
                "human:owner",
                "root",
                root_signer.kind,
                root_signer.signer_id,
                ("actor.authorize",),
            )
        ],
        "policy:delegation",
    )
    initialize_genesis(store, genesis)
    worker_key = ActorKey(
        "worker:writer",
        "worker-1",
        worker_signer.kind,
        worker_signer.signer_id,
        ("asset.publish",),
    )
    authorization = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id="human:owner",
        key_id="root",
        effects=[actor_authorization_effect(worker_key)],
        signer=root_signer,
    )
    trace = store if isinstance(store, SQLiteStore) else MemoryTraceStore()
    assert CandidateCommitter(store, trace).commit(authorization, genesis).accepted

    proposal = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=worker_key.actor_id,
        key_id=worker_key.key_id,
        effects=[
            CandidateEffect(
                "asset.propose",
                {"asset": {"name": "worker_result", "content": "signed"}},
            )
        ],
        signer=worker_signer,
    )
    decision = CandidateCommitter(store, trace).commit(proposal, genesis)

    assert decision.accepted is True
    assert (
        store.get_assets_by_name("worker_result")[0].created_by == worker_key.actor_id
    )


def test_actor_identity_cannot_be_rebound_to_another_public_key(store):
    root_signer = Ed25519Signer()
    genesis = create_genesis_manifest(
        "actor-conflict",
        [
            ActorKey(
                "human:owner",
                "root",
                root_signer.kind,
                root_signer.signer_id,
                ("actor.authorize",),
            )
        ],
        "policy:delegation",
    )
    initialize_genesis(store, genesis)
    trace = store if isinstance(store, SQLiteStore) else MemoryTraceStore()
    committer = CandidateCommitter(store, trace)

    for signer in (Ed25519Signer(), Ed25519Signer()):
        key = ActorKey(
            "worker:fixed",
            "key-1",
            signer.kind,
            signer.signer_id,
            ("asset.publish",),
        )
        candidate = create_candidate_proposal(
            domain_id=genesis.id,
            actor_id="human:owner",
            key_id="root",
            effects=[actor_authorization_effect(key)],
            signer=root_signer,
        )
        if not store.scan_runtime_records(record_type="actor.authorized"):
            assert committer.commit(candidate, genesis).accepted
        else:
            with pytest.raises(ImmutableRecordConflict, match="actor key"):
                committer.commit(candidate, genesis)


def test_asset_relation_candidate_materializes_authenticated_claim(store):
    genesis, candidate = _proposal(
        capabilities=("asset.relate",),
        effect=CandidateEffect(
            "asset.relate",
            {
                "claim": {
                    "source_asset_id": "asset:source",
                    "replacement_asset_id": "asset:replacement",
                    "definition_hash": "def:report",
                    "claim_type": "replacement",
                    "lineage_id": "lineage:report",
                }
            },
        ),
    )
    trace = store if isinstance(store, SQLiteStore) else MemoryTraceStore()

    decision = CandidateCommitter(store, trace).commit(
        candidate, genesis, verifier_factory=_verifier_factory
    )

    assert decision.accepted is True
    claims = store.get_claims_for_asset("asset:source")
    assert len(claims) == 1
    assert claims[0].replacement_asset_id == "asset:replacement"
    assert claims[0].signed_by == "human:owner"
    assert claims[0].provenance_seal == candidate.signature


def test_asset_relation_requires_dedicated_actor_capability():
    genesis, candidate = _proposal(
        capabilities=("asset.publish",),
        effect=CandidateEffect(
            "asset.relate",
            {
                "claim": {
                    "source_asset_id": "source",
                    "replacement_asset_id": "replacement",
                }
            },
        ),
    )

    decision = reduce_candidate(candidate, genesis, verifier_factory=_verifier_factory)

    assert decision.accepted is False
    assert "asset.relate" in str(decision.runtime_records[1].payload["reason"])


@pytest.fixture(params=["memory", "sqlite"])
def store(request):
    value = MemoryStore() if request.param == "memory" else SQLiteStore(":memory:")
    yield value
    if isinstance(value, SQLiteStore):
        value.close()


def test_committer_is_conformant_and_idempotent(store):
    genesis, candidate = _proposal()
    trace = store if isinstance(store, SQLiteStore) else MemoryTraceStore()
    committer = CandidateCommitter(store, trace)

    first = committer.commit(candidate, genesis, verifier_factory=_verifier_factory)
    revision = store.get_runtime_revision()
    second = committer.commit(candidate, genesis, verifier_factory=_verifier_factory)

    assert first.accepted is True
    assert second.accepted is True
    assert store.get_contract(_contract().id) == _contract()
    assert store.get_runtime_revision() == revision


@pytest.mark.parametrize(
    ("capabilities", "effect", "reason"),
    [
        ((), None, "lacks required capability"),
        (
            ("contract.publish",),
            CandidateEffect("asset.propose", {"name": "report"}),
            "lacks required capability",
        ),
        (
            ("contract.publish",),
            CandidateEffect(
                "contract.declare",
                {"contract": {**contract_to_dict(_contract()), "id": "forged"}},
            ),
            "canonical task:v3 identity",
        ),
    ],
)
def test_invalid_effects_are_visible_rejections(capabilities, effect, reason):
    genesis, candidate = _proposal(capabilities=capabilities, effect=effect)
    decision = reduce_candidate(candidate, genesis, verifier_factory=_verifier_factory)

    assert decision.accepted is False
    assert decision.contract is None
    rejection = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.rejected"
    )
    assert reason in rejection.payload["reason"]
    assert decision.trace_entries[0].authority_result == "rejected"


def test_authentication_failure_is_persisted_but_never_committed(store):
    genesis, candidate = _proposal()
    forged = replace(candidate, signature="forged")
    trace = store if isinstance(store, SQLiteStore) else MemoryTraceStore()

    decision = CandidateCommitter(store, trace).commit(
        forged, genesis, verifier_factory=_verifier_factory
    )

    assert decision.accepted is False
    assert store.get_contract(_contract().id) is None
    assert any(
        record.record_type == "candidate.authentication_rejected"
        for _, record in store.scan_runtime_records()
    )
    assert decision.trace_entries[0].event_type == "candidate_authentication_rejected"


def test_committer_reconstructs_genesis_from_store(store):
    genesis, candidate = _proposal()
    initialize_genesis(store, genesis)
    trace = store if isinstance(store, SQLiteStore) else MemoryTraceStore()

    decision = CandidateCommitter(store, trace).commit(
        candidate, verifier_factory=_verifier_factory
    )

    assert decision.accepted is True
    assert store.get_contract(_contract().id) == _contract()


def test_asset_effect_commits_fact_and_reduces_contract_completion(store):
    genesis, contract_candidate = _proposal()
    trace = store if isinstance(store, SQLiteStore) else MemoryTraceStore()
    committer = CandidateCommitter(store, trace)
    committer.commit(contract_candidate, genesis, verifier_factory=_verifier_factory)
    signer = _Signer()
    asset_candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id="human:owner",
        key_id="root",
        effects=[
            CandidateEffect(
                "asset.propose",
                {
                    "asset": {
                        "name": "report",
                        "content": "reviewed",
                        "origin": "human",
                        "trust_tier": "human",
                    }
                },
            )
        ],
        signer=signer,
    )

    first = committer.commit(
        asset_candidate,
        genesis,
        verifier_factory=_verifier_factory,
    )
    revision = store.get_runtime_revision()
    second = committer.commit(
        asset_candidate,
        genesis,
        verifier_factory=_verifier_factory,
    )

    assert first.accepted is True
    assert second.accepted is True
    assert store.has_asset_named("report")
    assert store.get_runtime_revision() == revision
    assert any(
        record.record_type == "lifecycle.terminal"
        and record.payload["contract_id"] == _contract().id
        and record.payload["terminal"] == "complete"
        for _, record in store.scan_runtime_records()
    )


def test_asset_effect_rejects_protected_namespace():
    genesis, candidate = _proposal(
        capabilities=("asset.publish",),
        effect=CandidateEffect(
            "asset.propose", {"asset": {"name": "_sys_forged", "content": "x"}}
        ),
    )

    decision = reduce_candidate(candidate, genesis, verifier_factory=_verifier_factory)

    assert decision.accepted is False
    assert "asset.publish.protected" in next(
        record.payload["reason"]
        for record in decision.runtime_records
        if record.record_type == "candidate.rejected"
    )

    privileged_genesis, privileged_candidate = _proposal(
        capabilities=("asset.publish", "asset.publish.protected"),
        effect=CandidateEffect(
            "asset.propose", {"asset": {"name": "_sys_allowed", "content": "x"}}
        ),
    )
    privileged = reduce_candidate(
        privileged_candidate,
        privileged_genesis,
        verifier_factory=_verifier_factory,
    )
    assert privileged.accepted is True


def test_contract_publisher_cannot_self_grant_protected_minting_authority():
    contract = _contract()
    protected = Contract(
        **{
            **contract_to_dict(contract),
            "id": hash_contract_v3(
                name=contract.name,
                description=contract.description,
                inputs=contract.inputs,
                outputs=("_sys_result",),
                activation=contract.activation,
                budget=contract.budget,
                tool_scope=contract.tool_scope,
                labels=contract.labels,
                origin=contract.origin,
                minting_authority=("_sys_result",),
            ),
            "outputs": ("_sys_result",),
            "minting_authority": ("_sys_result",),
        }
    )
    genesis, candidate = _proposal(
        capabilities=("contract.publish",),
        effect=CandidateEffect(
            "contract.declare", {"contract": contract_to_dict(protected)}
        ),
    )

    decision = reduce_candidate(candidate, genesis, verifier_factory=_verifier_factory)

    assert decision.accepted is False
    assert "contract.publish.protected" in next(
        record.payload["reason"]
        for record in decision.runtime_records
        if record.record_type == "candidate.rejected"
    )

    privileged_genesis, privileged_candidate = _proposal(
        capabilities=("contract.publish", "contract.publish.protected"),
        effect=CandidateEffect(
            "contract.declare", {"contract": contract_to_dict(protected)}
        ),
    )
    privileged = reduce_candidate(
        privileged_candidate,
        privileged_genesis,
        verifier_factory=_verifier_factory,
    )
    assert privileged.accepted is True


def test_commitment_kernel_has_no_concrete_store_dependency():
    source = (
        Path(__file__).resolve().parents[2] / "src/aigineering/core/commitment.py"
    ).read_text(encoding="utf-8")

    assert "sqlite_store" not in source


def test_concurrent_candidate_commit_is_database_idempotent(tmp_path):
    path = tmp_path / "shared.db"
    setup = SQLiteStore(str(path))
    genesis, contract_candidate = _proposal()
    initialize_genesis(setup, genesis)
    CandidateCommitter(setup, setup).commit(
        contract_candidate, verifier_factory=_verifier_factory
    )
    signer = _Signer()
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id="human:owner",
        key_id="root",
        effects=[
            CandidateEffect(
                "asset.propose",
                {"asset": {"name": "report", "content": "one fact"}},
            )
        ],
        signer=signer,
    )
    setup.close()

    def commit_once():
        store = SQLiteStore(str(path))
        try:
            return CandidateCommitter(store, store).commit(
                candidate, verifier_factory=_verifier_factory
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda _: commit_once(), range(2)))

    reopened = SQLiteStore(str(path))
    assert all(decision.accepted for decision in decisions)
    assert len(reopened.get_assets_by_name("report")) == 1
    candidate_records = [
        record
        for _, record in reopened.scan_runtime_records()
        if record.payload.get("candidate_id") == candidate.id
    ]
    assert [record.record_type for record in candidate_records].count(
        "candidate.received"
    ) == 1
    assert [record.record_type for record in candidate_records].count(
        "candidate.committed"
    ) == 1
    terminals = [
        record
        for _, record in reopened.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload["contract_id"] == _contract().id
    ]
    assert len(terminals) == 1
    reopened.close()
