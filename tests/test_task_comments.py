from __future__ import annotations

import pytest

from aigineering.core.candidate_publisher import CandidatePublisher, publish_effect
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.effect_projection import freeze_additional_effects
from aigineering.core.effect_projection import project_effect_batch
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins.collaboration import (
    COLLABORATION_EFFECTS,
    project_task_comments,
)
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    CandidateEffect,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    task_comment_effect,
)


def _setup(tmp_path, *, capabilities=("task.comment",)):
    store = SQLiteStore(db_path=str(tmp_path / "comments.sqlite"))
    signer = Ed25519Signer()
    effective_capabilities = tuple(sorted(set(capabilities) | {"contract.publish"}))
    key = ActorKey(
        "human:reviewer",
        "reviewer-key",
        signer.kind,
        signer.signer_id,
        effective_capabilities,
    )
    genesis = create_genesis_manifest("task-comments-test", (key,), "policy:test")
    initialize_genesis(store, genesis)
    contract = build_control_plane_contract(name="comment target", description="target")
    published = publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        contract_declaration_effect(contract),
        idempotency_key="publish-comment-target",
    )
    assert published.accepted
    return store, signer, key, genesis, contract


def _proposal(genesis, signer, key, effect, nonce, **kwargs):
    return create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=(effect,),
        signer=signer,
        idempotency_key=nonce,
        **kwargs,
    )


def _committer(store):
    return CandidateCommitter(store, store, additional_effects=COLLABORATION_EFFECTS)


def test_signed_comment_commits_and_rebuilds_without_changing_task(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    proposal = _proposal(
        genesis,
        signer,
        key,
        task_comment_effect(contract.id, "Please clarify the method."),
        "comment-1",
    )

    decision = _committer(store).commit(proposal, genesis)

    assert decision.accepted
    comment = next(
        r for r in decision.runtime_records if r.record_type == "task.commented"
    )
    assert dict(comment.payload) == {
        "actor_id": key.actor_id,
        "candidate_id": proposal.id,
        "contract_id": contract.id,
        "key_id": key.key_id,
        "reply_to": "",
        "text": "Please clarify the method.",
    }
    assert len(comment.causal_parents) == 1
    assert store.get_contract(contract.id) == contract
    assert store.scan_runtime_records(record_type="lifecycle.terminal") == []
    reopened = SQLiteStore(db_path=str(tmp_path / "comments.sqlite"))
    persisted = tuple(record for _, record in reopened.scan_runtime_records())
    assert project_task_comments(persisted, contract_id=contract.id) == (comment,)
    assert project_task_comments(persisted, contract_id="another") == ()


def test_plugin_effects_are_opt_in_and_host_mapping_is_frozen(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    proposal = _proposal(
        genesis,
        signer,
        key,
        task_comment_effect(contract.id, "Only when installed"),
        "opt-in",
    )

    default_decision = CandidateCommitter(store, store).commit(proposal, genesis)

    assert not default_decision.accepted
    assert "unsupported effect" in str(default_decision.runtime_records[-1].payload)
    assert store.scan_runtime_records(record_type="task.commented") == []
    injected = _committer(store).commit(
        _proposal(
            genesis,
            signer,
            key,
            task_comment_effect(contract.id, "Installed"),
            "opt-in-installed",
        ),
        genesis,
    )
    assert injected.accepted


def test_claim_bound_candidate_cannot_use_injected_comment_effect(tmp_path):
    _store, signer, key, genesis, contract = _setup(tmp_path)
    candidate = _proposal(
        genesis,
        signer,
        key,
        task_comment_effect(contract.id, "Claim bound"),
        "claim-bound-comment",
        claim_binding=CandidateClaimBinding(
            contract_id=contract.id,
            claim_id="lease:valid-test-shape",
            claim_epoch=1,
            package_id="pkg:valid-test-shape",
        ),
    )

    with pytest.raises(
        ValueError, match="claim-bound Candidate contains an unsupported effect"
    ):
        project_effect_batch(
            candidate,
            "receipt:valid-test",
            ("worker.submit", "task.comment"),
            EffectProjectionContext(contracts=(contract,)),
            additional_effects=COLLABORATION_EFFECTS,
        )


@pytest.mark.parametrize(
    "mapping",
    [
        {"asset.propose": ("task.comment", lambda *_: None)},
        {"task.comment": ("task.comment",)},
        {"task.comment": ("task.comment", None)},
        {"task.comment": ("", lambda *_: None)},
    ],
)
def test_additional_effect_mapping_rejects_override_and_malformed_handlers(
    mapping, tmp_path
):
    store, *_ = _setup(tmp_path)

    with pytest.raises(ValueError):
        CandidateCommitter(store, store, additional_effects=mapping)


def test_committer_and_publisher_snapshot_additional_effect_mapping(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    committer_mapping = dict(COLLABORATION_EFFECTS)
    committer = CandidateCommitter(store, store, additional_effects=committer_mapping)
    committer_mapping.clear()
    committer_result = committer.commit(
        _proposal(
            genesis,
            signer,
            key,
            task_comment_effect(contract.id, "committer snapshot"),
            "snapshot-committer",
        ),
        genesis,
    )
    assert committer_result.accepted

    publisher_mapping = dict(COLLABORATION_EFFECTS)
    publisher = CandidatePublisher(
        store, store, genesis, key, signer, additional_effects=publisher_mapping
    )
    publisher_mapping.clear()
    publisher_result = publisher.publish(
        (task_comment_effect(contract.id, "publisher snapshot"),),
        idempotency_key="snapshot-publisher",
    )
    assert publisher_result.accepted


def test_freeze_additional_effects_returns_immutable_copy():
    source = dict(COLLABORATION_EFFECTS)
    expected = {
        name: (handler.capability, handler.projector)
        for name, handler in freeze_additional_effects(source).items()
    }
    frozen = freeze_additional_effects(source)
    source.clear()

    assert set(frozen) == {
        "task.comment",
        "task.block",
        "task.unblock",
        "task.promote",
    }
    for name, (capability, projector) in expected.items():
        assert frozen[name].capability == capability
        assert frozen[name].projector is projector
        assert frozen[name].emitted_fact_types == frozenset()
        assert frozen[name].assignment_admission.value == "none"
    with pytest.raises(TypeError):
        frozen["another.effect"] = frozen["task.comment"]


def test_comments_allow_replies_only_to_committed_comment_on_same_task(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    committer = _committer(store)
    first = _proposal(
        genesis, signer, key, task_comment_effect(contract.id, "Question"), "first"
    )
    first_decision = committer.commit(first, genesis)
    assert first_decision.accepted
    first_comment = next(
        record
        for record in first_decision.runtime_records
        if record.record_type == "task.commented"
    )
    reply = _proposal(
        genesis,
        signer,
        key,
        task_comment_effect(contract.id, "Answer", reply_to=first_comment.id),
        "reply",
    )

    reply_decision = committer.commit(reply, genesis)

    assert reply_decision.accepted
    reply_record = next(
        record
        for record in reply_decision.runtime_records
        if record.record_type == "task.commented"
    )
    assert reply_record.payload["reply_to"] == first_comment.id
    assert first_comment.id in reply_record.causal_parents


def test_task_comment_rejects_unauthorized_actor_and_forged_payload_fields(tmp_path):
    store, signer, key, genesis, contract = _setup(
        tmp_path, capabilities=("worker.submit",)
    )
    committer = _committer(store)
    proposal = _proposal(
        genesis,
        signer,
        key,
        task_comment_effect(contract.id, "No authority"),
        "unauthorized",
    )
    denied = committer.commit(proposal, genesis)
    assert not denied.accepted
    assert store.scan_runtime_records(record_type="task.commented") == []

    forged = _proposal(
        genesis,
        signer,
        key,
        # CandidateEffect's immutable payload still signs this extra field;
        # the projector rejects it instead of trusting the caller's author.
        CandidateEffect(
            "task.comment",
            {
                "contract_id": contract.id,
                "reply_to": "",
                "text": "forged",
                "actor_id": "human:admin",
            },
        ),
        "forged-field",
    )
    denied_forgery = committer.commit(forged, genesis)
    assert not denied_forgery.accepted
    assert store.scan_runtime_records(record_type="task.commented") == []


def test_task_comment_rejects_unknown_target_bad_reply_and_invalid_text(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    committer = _committer(store)
    other = build_control_plane_contract(name="other target", description="other")
    assert publish_effect(
        store,
        store,
        genesis,
        key,
        signer,
        contract_declaration_effect(other),
        idempotency_key="publish-other-target",
    ).accepted
    other_comment = _proposal(
        genesis,
        signer,
        key,
        task_comment_effect(other.id, "Other task"),
        "other-comment",
    )
    other_decision = committer.commit(other_comment, genesis)
    assert other_decision.accepted
    other_record = next(
        record
        for record in other_decision.runtime_records
        if record.record_type == "task.commented"
    )
    effects = (
        task_comment_effect("missing-task", "unknown"),
        task_comment_effect(contract.id, "cross task", reply_to="record:missing"),
        task_comment_effect(contract.id, "cross task", reply_to=other_record.id),
        task_comment_effect(contract.id, "  "),
        task_comment_effect(contract.id, "é" * 32769),
        CandidateEffect(
            "task.comment", {"contract_id": contract.id, "reply_to": "", "text": 7}
        ),
        CandidateEffect(
            "task.comment", {"contract_id": contract.id, "reply_to": None, "text": "x"}
        ),
    )
    for index, effect in enumerate(effects):
        decision = committer.commit(
            _proposal(genesis, signer, key, effect, f"invalid-{index}"), genesis
        )
        assert not decision.accepted
    comments = store.scan_runtime_records(record_type="task.commented")
    assert [record.id for _, record in comments] == [other_record.id]


def test_terminal_task_remains_commentable_and_repeat_commit_is_idempotent(tmp_path):
    store, signer, key, genesis, contract = _setup(tmp_path)
    store.append_runtime_record(
        create_terminal_record(
            contract.id, "complete", actor_id="system", reason="done"
        )
    )
    proposal = _proposal(
        genesis,
        signer,
        key,
        task_comment_effect(contract.id, "After completion"),
        "terminal-comment",
    )
    committer = _committer(store)

    first = committer.commit(proposal, genesis)
    second = committer.commit(proposal, genesis)

    assert first.accepted and second.accepted
    assert len(store.scan_runtime_records(record_type="task.commented")) == 1
    assert len(store.scan_runtime_records(record_type="lifecycle.terminal")) == 1
