"""Conformance tests for the first Candidate commitment vertical slice."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from aigineering.core.commitment import CandidateCommitter, reduce_candidate
from aigineering.core.ids import hash_contract_v3
from aigineering.core.signing import Signer, Verifier
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    create_candidate_proposal,
    create_genesis_manifest,
)
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


def _proposal(*, capabilities=("contract.publish",), effect=None):
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
            "unsupported effect type",
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


def test_commitment_kernel_has_no_concrete_store_dependency():
    source = (
        Path(__file__).resolve().parents[2] / "src/aigineering/core/commitment.py"
    ).read_text(encoding="utf-8")

    assert "sqlite_store" not in source
