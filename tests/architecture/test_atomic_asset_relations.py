"""A batch-local relation reference never creates a second publication path."""

from dataclasses import replace
import shutil

import pytest
from conftest import candidate_runtime

from aigineering.core import commitment
from aigineering.core.asset_versions import create_replacement_claim, create_slice_asset
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.signing import Ed25519Signer
from aigineering.core.verification import verify_replacement_claims
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    create_candidate_proposal,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    replacement_claim_effect,
)
from aigineering.protocol.types import Asset


def _prepare(store, *, disclosure="original", promptable=True):
    runtime = candidate_runtime(store)
    source = runtime.accept_asset(
        Asset(
            id="fixture:source",
            name="source",
            content="first\ne\u0301数值42\nlast\n",
            disclosure_view=disclosure,
            promptable=promptable,
            trust_tier="human",
        )
    )
    parent = runtime.accept_contract(
        build_control_plane_contract(
            name="report",
            outputs=("excerpt",),
            budget=5,
        )
    )
    child = runtime.accept_contract(
        replace(
            build_control_plane_contract(
                name="unfinished",
                outputs=("later",),
                budget=1,
            ),
            id="fixture:child",
            parent_id=parent.id,
        )
    )
    sliced = create_slice_asset(source, slice_name="excerpt", range_spec="lines:2-2")
    claim = create_replacement_claim(
        source.id,
        sliced.id,
        definition_hash=sliced.definition_hash,
        claim_type="slice",
        lineage_id=source.id,
        range_spec="lines:2-2",
    )
    effects = (
        asset_proposal_effect(sliced),
        replacement_claim_effect(claim, replacement_effect_index=0),
    )
    return runtime, source, parent, child, effects


def _candidate(runtime, effects):
    return create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        signer=runtime.signer,
        effects=effects,
        idempotency_key="slice-atomic",
    )


@pytest.mark.parametrize(
    "disclosure,promptable", [("original", True), ("sealed", False)]
)
def test_slice_closure_exact_replay_reopen_and_rebuild(
    tmp_path, monkeypatch, disclosure, promptable
):
    path = tmp_path / "slice.db"
    with SQLiteStore(path) as store:
        runtime, source, parent, child, effects = _prepare(
            store, disclosure=disclosure, promptable=promptable
        )
        candidate = _candidate(runtime, effects)
        before = store.get_runtime_revision()
        decision = commitment.CandidateCommitter(store, store).commit(
            candidate, runtime.genesis
        )
        assert decision.accepted
        sliced = decision.assets[0]
        assert sliced.content == "e\u0301数值42\n"  # no NFC rewrite
        assert sliced.disclosure_view == disclosure
        assert sliced.promptable is promptable
        assert sliced.trust_tier == source.trust_tier
        assert sliced.lineage_id == source.id
        claims = store.get_claims_for_replacement_asset(sliced.id)
        assert len(claims) == 1
        assert verify_replacement_claims(store, claims)["fail_count"] == 0
        assert store.get_asset(source.id) == source
        records = [r for _, r in store.scan_runtime_records(after_revision=before)]
        (receipt,) = [r for r in records if r.record_type == "candidate.received"]
        (head,) = [r for r in records if r.record_type == "candidate.committed"]
        member_ids = set(head.payload["committed_record_ids"])
        assert set(r.id for r in records if r not in (receipt, head)) == member_ids
        (relation,) = [r for r in records if r.record_type == "replacement.claimed"]
        assert relation.payload["claim"]["replacement_asset_id"] == sliced.id
        assert "replacement_effect_index" not in relation.payload
        assert relation.causal_parents == (receipt.id,)
        assert (
            RuntimeProjection(store, store).contract_view(parent).terminal == "complete"
        )
        assert (
            RuntimeProjection(store, store).contract_view(child).terminal == "cancelled"
        )
        revision = store.get_runtime_revision()

        def forbidden(*args, **kwargs):
            raise AssertionError("exact replay cannot invoke projection")

        with monkeypatch.context() as patch:
            patch.setattr(commitment, "project_effect_batch", forbidden)
            replay = commitment.CandidateCommitter(store, store).commit(
                candidate, runtime.genesis
            )
        assert replay.accepted and replay.assets == decision.assets
        assert store.get_runtime_revision() == revision
        digest = store.runtime_materialization_digest()
    shutil.copyfile(path, tmp_path / "rebuild.db")
    with SQLiteStore(path) as reopened, SQLiteStore(tmp_path / "rebuild.db") as rebuilt:
        assert reopened.get_asset(sliced.id) == sliced
        assert reopened.get_claims_for_replacement_asset(sliced.id) == claims
        assert rebuilt.rebuild_runtime_materializations(strict=True) == digest
        assert reopened.runtime_materialization_digest() == digest


@pytest.mark.parametrize("index", [True, "0", -1, 1, 500, None])
def test_invalid_static_reference_rejects_entire_batch(tmp_path, index):
    with SQLiteStore(tmp_path / "invalid.db") as store:
        runtime, source, parent, child, effects = _prepare(store)
        relation = CandidateEffect(
            "asset.relate", {**effects[1].payload, "replacement_effect_index": index}
        )
        decision = commitment.CandidateCommitter(store, store).commit(
            _candidate(runtime, (effects[0], relation)), runtime.genesis
        )
        assert not decision.accepted
        assert "replacement_effect_index" in str(
            next(
                r.payload["reason"]
                for r in decision.runtime_records
                if r.record_type == "candidate.rejected"
            )
        )
        assert not store.get_assets_by_name("excerpt")
        assert not store.get_claims_for_asset(source.id)
        assert RuntimeProjection(store, store).contract_view(parent).terminal is None
        assert RuntimeProjection(store, store).contract_view(child).terminal is None


def test_reference_must_not_mix_exact_id_or_target_a_nonproposal(tmp_path):
    with SQLiteStore(tmp_path / "mixed.db") as store:
        runtime, source, _, _, effects = _prepare(store)
        payload = dict(effects[1].payload)
        payload["claim"] = {**payload["claim"], "replacement_asset_id": source.id}
        decision = commitment.CandidateCommitter(store, store).commit(
            _candidate(runtime, (effects[0], CandidateEffect("asset.relate", payload))),
            runtime.genesis,
        )
        assert not decision.accepted
        assert "without replacement_asset_id" in str(
            next(
                r.payload["reason"]
                for r in decision.runtime_records
                if r.record_type == "candidate.rejected"
            )
        )
        # A prior ordinary relation cannot become an indirect alias.
        exact = CandidateEffect("asset.relate", {"claim": payload["claim"]})
        candidate = _candidate(runtime, (exact, effects[1]))
        decision = commitment.CandidateCommitter(store, store).commit(
            candidate, runtime.genesis
        )
        assert not decision.accepted
        assert "prior asset.propose" in str(
            next(
                r.payload["reason"]
                for r in decision.runtime_records
                if r.record_type == "candidate.rejected"
            )
        )
        assert not store.get_assets_by_name("excerpt")
        assert not store.get_claims_for_asset(source.id)


def test_local_reference_does_not_grant_relation_authority(tmp_path):
    with SQLiteStore(tmp_path / "authority.db") as store:
        runtime, source, parent, _, effects = _prepare(store)
        signer = Ed25519Signer()
        key = ActorKey(
            "publisher", "key-1", signer.kind, signer.signer_id, ("asset.publish",)
        )
        runtime.authorize_actor(key)
        candidate = create_candidate_proposal(
            domain_id=runtime.genesis.id,
            actor_id=key.actor_id,
            key_id=key.key_id,
            signer=signer,
            effects=effects,
            idempotency_key="no-relation-authority",
        )
        decision = commitment.CandidateCommitter(store, store).commit(
            candidate, runtime.genesis
        )
        assert not decision.accepted
        assert any(
            "asset.relate" in str(r.payload.get("reason", ""))
            for r in decision.runtime_records
        )
        assert not store.get_assets_by_name("excerpt")
        assert not store.get_claims_for_asset(source.id)
        assert RuntimeProjection(store, store).contract_view(parent).terminal is None


def test_mid_commit_interruption_rolls_back_slice_relation_and_completion(
    tmp_path, monkeypatch
):
    with SQLiteStore(tmp_path / "interrupted.db") as store:
        runtime, source, parent, child, effects = _prepare(store)
        before = store.get_runtime_revision()
        insert = store._insert_runtime_record

        def interrupted(record):
            if record.record_type == "replacement.claimed":
                raise RuntimeError("injected slice interruption")
            return insert(record)

        monkeypatch.setattr(store, "_insert_runtime_record", interrupted)
        with pytest.raises(RuntimeError, match="injected slice interruption"):
            commitment.CandidateCommitter(store, store).commit(
                _candidate(runtime, effects), runtime.genesis
            )
        assert store.get_runtime_revision() == before
        assert not store.get_assets_by_name("excerpt")
        assert not store.get_claims_for_asset(source.id)
        assert store.get_asset(source.id) == source
        assert RuntimeProjection(store, store).contract_view(parent).terminal is None
        assert RuntimeProjection(store, store).contract_view(child).terminal is None
