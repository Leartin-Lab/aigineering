"""Conservation and schema-floor tests for paired causal allowance returns."""

from __future__ import annotations

import sqlite3

import pytest

from aigineering.core.causal_allowance import (
    CausalAllowanceConflict,
    allowance_available,
    allowance_purpose,
    allowance_reservation_id,
    project_causal_allowance_returns,
    project_contract_allowance_records,
    terminal_allowance_records,
    validate_allowance_commit,
    validate_causal_allowance_returns,
)
from aigineering.core.effect_projection import EffectHandler, EffectProjection
from aigineering.core.ids import hash_contract_v3
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_cancellation_effect,
    contract_declaration_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import contract_to_dict
from aigineering.core.candidate_publisher import publish_effect, publish_effects


def _contract(name: str, *, budget: int, parent_id: str | None = None) -> Contract:
    fields = {
        "name": name,
        "description": f"Complete {name}.",
        "inputs": (),
        "outputs": (f"{name}.out",),
        "activation": "",
        "budget": budget,
        "tool_scope": (),
        "labels": (),
        "origin": "human" if parent_id is None else "plan",
        "parent_id": parent_id,
    }
    return Contract(id=hash_contract_v3(**fields), **fields)


def _world(store=None):
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:allowance-return-test",
        "allowance-key",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "contract.cancel"),
    )
    genesis = create_genesis_manifest("allowance-return-test", (actor,), "policy:test")
    store = store or MemoryStore()
    trace = MemoryTraceStore()
    return signer, actor, genesis, store, trace


def _publish(store, trace, genesis, actor, signer, contract, key):
    decision = publish_effect(
        store,
        trace,
        genesis,
        actor,
        signer,
        contract_declaration_effect(contract),
        idempotency_key=key,
    )
    assert decision.accepted, decision


def _records(store, record_type):
    return tuple(
        record for _, record in store.scan_runtime_records(record_type=record_type)
    )


def _candidate_facts(candidate_id: str):
    receipt = create_runtime_record(
        "candidate.received", {"candidate_id": candidate_id, "actor_id": "human:test"}
    )
    return receipt


def test_extinguishment_requires_one_exact_terminal_proof_and_positive_amount():
    signer, actor, genesis, store, trace = _world()
    contract = _contract("terminal-proof", budget=5)
    _publish(store, trace, genesis, actor, signer, contract, "terminal-proof-root")
    existing = tuple(record for _, record in store.scan_runtime_records())
    terminal = create_terminal_record(contract.id, "failed")
    valid = create_runtime_record(
        "allowance.extinguished",
        {"amount": 5, "contract_id": contract.id, "reason": "failed"},
        causal_parents=(terminal.id,),
    )

    validate_allowance_commit((contract,), existing, (terminal, valid))
    validate_allowance_commit((contract,), existing, (terminal, valid, valid))

    invalid_records = (
        (valid,),
        (
            create_terminal_record("task:another-contract", "failed"),
            valid,
        ),
        (
            terminal,
            create_runtime_record(
                "allowance.extinguished",
                {"amount": 5, "contract_id": contract.id, "reason": "cancelled"},
                causal_parents=(terminal.id,),
            ),
        ),
        (
            terminal,
            create_runtime_record(
                "allowance.extinguished",
                {"amount": 5, "contract_id": contract.id, "reason": "failed"},
                causal_parents=(terminal.id, "record:extra-parent"),
            ),
        ),
        (
            create_runtime_record(
                "lifecycle.terminal",
                {"contract_id": contract.id, "terminal": "not-a-terminal"},
            ),
            valid,
        ),
        (
            terminal,
            create_terminal_record(contract.id, "cancelled"),
            valid,
        ),
        *(
            (
                terminal,
                create_runtime_record(
                    "allowance.extinguished",
                    {"amount": amount, "contract_id": contract.id, "reason": "failed"},
                    causal_parents=(terminal.id,),
                ),
            )
            for amount in (0, -1, 5.0, "5")
        ),
    )
    for pending in invalid_records:
        with pytest.raises((CausalAllowanceConflict, ValueError)):
            validate_allowance_commit((contract,), existing, pending)

    version_two_duplicate = create_runtime_record(
        "allowance.extinguished",
        {"amount": 5, "contract_id": contract.id, "reason": "failed"},
        causal_parents=(terminal.id,),
        schema_version=2,
    )
    with pytest.raises(CausalAllowanceConflict):
        validate_allowance_commit(
            (contract,), existing, (terminal, valid, version_two_duplicate)
        )

    unit_contract = _contract("terminal-proof-unit", budget=1)
    _publish(store, trace, genesis, actor, signer, unit_contract, "terminal-proof-unit")
    unit_existing = tuple(record for _, record in store.scan_runtime_records())
    unit_terminal = create_terminal_record(unit_contract.id, "complete")
    unit_bool_extinguishment = create_runtime_record(
        "allowance.extinguished",
        {"amount": True, "contract_id": unit_contract.id, "reason": "complete"},
        causal_parents=(unit_terminal.id,),
    )
    with pytest.raises(CausalAllowanceConflict):
        validate_allowance_commit(
            (unit_contract,),
            unit_existing,
            (unit_terminal, unit_bool_extinguishment),
        )

    already_extinguished = (*existing, terminal, valid)
    # Exact replay adds no second debit; historical facts are not retroactively
    # revalidated when a later unrelated commit is checked.
    validate_allowance_commit((contract,), already_extinguished, (valid,))
    validate_allowance_commit((contract,), already_extinguished, ())
    with pytest.raises((CausalAllowanceConflict, ValueError)):
        validate_allowance_commit(
            (contract,),
            already_extinguished,
            (
                create_runtime_record(
                    "allowance.extinguished",
                    {"amount": 5, "contract_id": contract.id, "reason": "failed"},
                    causal_parents=(terminal.id, "different-parent"),
                ),
            ),
        )
    malformed_historical = create_runtime_record(
        "allowance.extinguished",
        {"amount": 5, "contract_id": contract.id, "reason": "unpaired"},
        causal_parents=("record:historical-nonterminal",),
    )
    validate_allowance_commit((contract,), (*existing, malformed_historical), ())


def test_signed_injected_extinguishment_without_terminal_is_audited_and_rejected(
    tmp_path,
):
    store = SQLiteStore(tmp_path / "unpaired-extinguishment.sqlite")
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:allowance-audit-injected",
        "allowance-audit-key",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "contract.cancel", "audit.extinguish"),
    )
    genesis = create_genesis_manifest(
        "allowance-audit-injected", (actor,), "policy:allowance-audit"
    )
    from aigineering.core.domain import initialize_genesis

    initialize_genesis(store, genesis)
    trace = MemoryTraceStore()
    contract = _contract("signed-unpaired-extinguishment", budget=5)
    _publish(
        store,
        trace,
        genesis,
        actor,
        signer,
        contract,
        "signed-unpaired-extinguishment-root",
    )

    def projector(effect, candidate, receipt_id, context):
        del effect, candidate, context
        extinguished = create_runtime_record(
            "allowance.extinguished",
            {"amount": 5, "contract_id": contract.id, "reason": "failed"},
            causal_parents=(receipt_id,),
        )
        return EffectProjection(records=(extinguished,), relation_target=None)

    handler = EffectHandler(
        "audit.extinguish",
        projector,
        frozenset({"allowance.extinguished"}),
        direct_output_fact_types=frozenset({"allowance.extinguished"}),
        reserved_fact_types=frozenset({"allowance.extinguished"}),
    )
    decision = publish_effect(
        store,
        trace,
        genesis,
        actor,
        signer,
        CandidateEffect("audit.extinguish", {"contract_id": contract.id}),
        idempotency_key="signed-unpaired-extinguishment",
        additional_effects={"audit.extinguish": handler},
    )

    assert not decision.accepted
    assert any(
        "terminal" in str(record.payload.get("reason", ""))
        for record in decision.runtime_records
        if record.record_type == "candidate.rejected"
    )
    assert _records(store, "allowance.extinguished") == ()
    assert any(
        record.payload.get("candidate_id") == decision.candidate_id
        for record in _records(store, "candidate.rejected")
    )
    assert any(
        record.payload["trace"]["parent_id"] == decision.candidate_id
        and record.payload["trace"]["event_type"] == "candidate_rejected"
        for record in _records(store, "trace.recorded")
    )
    store.close()


def test_sqlite_terminal_allowance_revalidation_includes_main_reservation(tmp_path):
    path = str(tmp_path / "terminal-allowance-interleave.sqlite")
    store = SQLiteStore(path)
    writer = SQLiteStore(path)
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:allowance-interleave",
        "interleave-key",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "contract.cancel", "asset.publish"),
    )
    genesis = create_genesis_manifest(
        "allowance-interleave", (actor,), "policy:interleave"
    )
    from aigineering.core.domain import initialize_genesis

    initialize_genesis(store, genesis)
    trace = MemoryTraceStore()
    parent_fields = {
        "name": "allowance-interleave-parent",
        "description": "two outputs completed across writers",
        "inputs": (),
        "outputs": ("allowance-interleave-a", "allowance-interleave-b"),
        "activation": "",
        "budget": 10,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
        "parent_id": None,
    }
    parent = Contract(id=hash_contract_v3(**parent_fields), **parent_fields)
    _publish(store, trace, genesis, actor, signer, parent, "interleave-parent")
    child = _contract("allowance-interleave-child", budget=4, parent_id=parent.id)
    asset_a = Asset(id="", name=parent.outputs[0], content="part-a")
    asset_b = Asset(id="", name=parent.outputs[1], content="part-b")
    interleaved = False
    commit_main = store.commit_ingress_batch

    def publish_other_output(**kwargs):
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            concurrent = publish_effect(
                writer,
                writer,
                genesis,
                actor,
                signer,
                asset_proposal_effect(asset_b),
                idempotency_key="interleave-output-b",
            )
            assert concurrent.accepted
        return commit_main(**kwargs)

    store.commit_ingress_batch = publish_other_output
    result = publish_effects(
        store,
        trace,
        genesis,
        actor,
        signer,
        (contract_declaration_effect(child), asset_proposal_effect(asset_a)),
        idempotency_key="interleave-child-output-a",
    )
    assert interleaved
    assert result.accepted, result
    extinguishments_before_replay = _records(store, "allowance.extinguished")
    replay = publish_effects(
        store,
        trace,
        genesis,
        actor,
        signer,
        (contract_declaration_effect(child), asset_proposal_effect(asset_a)),
        idempotency_key="interleave-child-output-a",
    )
    assert replay.accepted, replay
    assert _records(store, "allowance.extinguished") == extinguishments_before_replay
    assert store.get_contract(child.id) == child
    assert {asset.name for asset in store.get_all_assets()} == set(parent.outputs)
    terminals = {
        record.payload["contract_id"]: record.payload["terminal"]
        for record in _records(store, "lifecycle.terminal")
    }
    assert terminals[parent.id] == "complete"
    assert terminals[child.id] == "cancelled"
    extinguished = _records(store, "allowance.extinguished")
    parent_ext = next(
        record for record in extinguished if record.payload["contract_id"] == parent.id
    )
    assert parent_ext.payload["amount"] == 6
    assert parent_ext.causal_parents == tuple(
        record.id
        for record in _records(store, "lifecycle.terminal")
        if record.payload.get("contract_id") == parent.id
    )
    child_ext = next(
        record for record in extinguished if record.payload["contract_id"] == child.id
    )
    assert child_ext.payload["amount"] == 4
    before_rebuild = store.runtime_materialization_digest()
    assert store.rebuild_runtime_materializations(strict=True) == before_rebuild
    store.close()
    writer.close()
    reopened = SQLiteStore(path)
    try:
        assert reopened.runtime_materialization_digest() == before_rebuild
        assert reopened.rebuild_runtime_materializations(strict=True) == before_rebuild
        assert len(_records(reopened, "allowance.extinguished")) == 2
        assert reopened.get_contract(child.id) == child
    finally:
        reopened.close()


def _writer_batch(store, source, donors, successor, candidate_id):
    receipt = _candidate_facts(candidate_id)
    cancellations = tuple(
        create_terminal_record(
            contract.id,
            "cancelled",
            actor_id="human:test",
            causal_parents=(receipt.id,),
        )
        for contract in (source, *donors)
    )
    existing_records = tuple(record for _, record in store.scan_runtime_records())
    contracts = tuple(store.get_all_contracts())
    returns = project_causal_allowance_returns(
        source,
        donors,
        contracts,
        existing_records,
        (receipt, *cancellations),
        candidate_id=candidate_id,
        receipt_id=receipt.id,
    )
    grants = project_contract_allowance_records(
        (successor,),
        contracts,
        existing_records + returns,
        causal_parent=receipt.id,
    )
    declaration = create_runtime_record(
        "contract.declared",
        {"candidate_id": candidate_id, "contract": contract_to_dict(successor)},
        causal_parents=(receipt.id,),
    )
    projected = (receipt, declaration, *cancellations, *returns, *grants)
    head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate_id,
            "committed_record_ids": [record.id for record in projected[1:]],
        },
        causal_parents=(receipt.id, *(record.id for record in projected[1:])),
    )
    pending = (*projected, head)
    validate_allowance_commit(
        (*contracts, successor),
        existing_records,
        pending,
        candidate_id=candidate_id,
    )
    return receipt, cancellations, returns, grants, pending


def test_returns_debit_live_donors_and_fund_one_successor(tmp_path):
    signer, actor, genesis, store, trace = _world()
    source = _contract("source", budget=10)
    child = _contract("child", budget=6, parent_id=source.id)
    grandchild = _contract("grandchild", budget=2, parent_id=child.id)
    _publish(store, trace, genesis, actor, signer, source, "source")
    _publish(store, trace, genesis, actor, signer, child, "child")
    _publish(store, trace, genesis, actor, signer, grandchild, "grandchild")
    successor = _contract("successor", budget=10, parent_id=source.id)

    _, _, returns, grants, pending = _writer_batch(
        store, source, (child, grandchild), successor, "tree-transfer"
    )

    assert {
        record.payload["from_contract_id"]: record.payload["amount"]
        for record in returns
    } == {
        child.id: 4,
        grandchild.id: 2,
    }
    assert sum(record.payload["amount"] for record in returns) == 6
    assert [
        record.payload["amount"]
        for record in grants
        if record.record_type == "allowance.reserved"
    ] == [10]
    all_records = (
        _records(store, "allowance.granted")
        + _records(store, "allowance.reserved")
        + returns
        + tuple(grants)
    )
    assert allowance_available(child, all_records) == 0
    assert allowance_available(grandchild, all_records) == 0
    assert allowance_available(source, all_records) == 0
    assert (
        validate_causal_allowance_returns(
            (*store.get_all_contracts(), successor),
            tuple(record for _, record in store.scan_runtime_records()),
            pending,
            candidate_id="tree-transfer",
            require_committed_head=True,
        )[source.id]
        == 6
    )


def test_live_grandchild_below_terminal_child_returns_without_reviving_child():
    signer, actor, genesis, store, trace = _world()
    source = _contract("closed-branch-source", budget=10)
    child = _contract("closed-branch", budget=6, parent_id=source.id)
    grandchild = _contract("live-leaf", budget=2, parent_id=child.id)
    _publish(store, trace, genesis, actor, signer, source, "closed-root")
    _publish(store, trace, genesis, actor, signer, child, "closed-child")
    _publish(store, trace, genesis, actor, signer, grandchild, "live-grandchild")
    cancelled = publish_effect(
        store,
        trace,
        genesis,
        actor,
        signer,
        # A terminal intermediate cannot donate its previously extinguished balance.
        contract_cancellation_effect(child.id, "closed before transfer"),
        idempotency_key="close-middle",
    )
    assert cancelled.accepted
    old_extinguishment = next(
        record
        for record in _records(store, "allowance.extinguished")
        if record.payload["contract_id"] == child.id
    )
    successor = _contract("new-source", budget=6, parent_id=source.id)

    _, cancellations, returns, grants, _ = _writer_batch(
        store, source, (grandchild,), successor, "transfer-live-grandchild"
    )

    assert len(returns) == 1 and returns[0].payload["amount"] == 2
    assert returns[0].payload["from_contract_id"] == grandchild.id
    assert child.id not in {record.payload["contract_id"] for record in cancellations}
    assert old_extinguishment in _records(store, "allowance.extinguished")
    assert [
        record.payload["amount"]
        for record in grants
        if record.record_type == "allowance.reserved"
    ] == [6]


def test_return_rejects_nonancestor_bad_donor_and_missing_funding_evidence():
    signer, actor, genesis, store, trace = _world()
    source = _contract("funding-source", budget=8)
    other = _contract("unrelated", budget=4)
    donor = _contract("funded-donor", budget=3, parent_id=source.id)
    for contract, key in ((source, "funding"), (other, "unrelated"), (donor, "donor")):
        _publish(store, trace, genesis, actor, signer, contract, key)
    receipt = _candidate_facts("bad-return")
    cancellation = create_terminal_record(
        donor.id, "cancelled", actor_id=actor.actor_id, causal_parents=(receipt.id,)
    )
    existing = tuple(record for _, record in store.scan_runtime_records())
    contracts = tuple(store.get_all_contracts())

    with pytest.raises(ValueError, match="strict ancestor"):
        project_causal_allowance_returns(
            other,
            (donor,),
            contracts,
            existing,
            (receipt, cancellation),
            candidate_id="bad-return",
            receipt_id=receipt.id,
        )
    with pytest.raises(ValueError, match="pending cancellation"):
        project_causal_allowance_returns(
            source,
            (donor,),
            contracts,
            existing,
            (receipt,),
            candidate_id="bad-return",
            receipt_id=receipt.id,
        )
    without_grant = tuple(
        record
        for record in existing
        if not (
            record.record_type == "allowance.granted"
            and record.payload.get("contract_id") == donor.id
        )
    )
    with pytest.raises(ValueError, match="funding path"):
        project_causal_allowance_returns(
            source,
            (donor,),
            contracts,
            without_grant,
            (receipt, cancellation),
            candidate_id="bad-return",
            receipt_id=receipt.id,
        )


def test_commit_gate_requires_receipt_head_and_rejects_excess_or_duplicate_returns():
    signer, actor, genesis, store, trace = _world()
    source = _contract("commit-source", budget=5)
    donor = _contract("commit-donor", budget=4, parent_id=source.id)
    _publish(store, trace, genesis, actor, signer, source, "commit-source")
    _publish(store, trace, genesis, actor, signer, donor, "commit-donor")
    successor = _contract("commit-successor", budget=5, parent_id=source.id)
    receipt, cancellations, returns, grants, pending = _writer_batch(
        store, source, (donor,), successor, "commit-transfer"
    )

    with pytest.raises(CausalAllowanceConflict, match="Candidate head"):
        validate_causal_allowance_returns(
            (*store.get_all_contracts(), successor),
            tuple(record for _, record in store.scan_runtime_records()),
            tuple(
                record
                for record in pending
                if record.record_type != "candidate.committed"
            ),
            candidate_id="commit-transfer",
            require_committed_head=True,
        )
    forged = create_runtime_record(
        "allowance.returned",
        {
            **dict(returns[0].payload),
            "amount": int(returns[0].payload["amount"]) + 1,
        },
        causal_parents=returns[0].causal_parents,
    )
    with pytest.raises(CausalAllowanceConflict, match="exceeds donor"):
        validate_causal_allowance_returns(
            (*store.get_all_contracts(), successor),
            tuple(record for _, record in store.scan_runtime_records()),
            (
                *tuple(record for record in pending if record.id != returns[0].id),
                forged,
            ),
            candidate_id="commit-transfer",
        )
    assert receipt.id in cancellations[0].causal_parents
    assert grants


def test_exact_return_projection_replay_is_idempotent_and_never_a_new_credit():
    signer, actor, genesis, store, trace = _world()
    source = _contract("replay-source", budget=8)
    donor = _contract("replay-donor", budget=4, parent_id=source.id)
    _publish(store, trace, genesis, actor, signer, source, "replay-source")
    _publish(store, trace, genesis, actor, signer, donor, "replay-donor")
    successor = _contract("replay-successor", budget=8, parent_id=source.id)
    _, _, returns, _, pending = _writer_batch(
        store, source, (donor,), successor, "return-replay"
    )
    contracts = (*store.get_all_contracts(), successor)

    replay_credits = validate_causal_allowance_returns(
        contracts,
        pending,
        pending,
        candidate_id="return-replay",
        require_committed_head=False,
    )

    assert replay_credits == {}
    assert len(returns) == 1
    with pytest.raises(ValueError, match="causal allowance exceeded"):
        project_contract_allowance_records(
            (_contract("unrelated-replay-child", budget=5, parent_id=source.id),),
            contracts,
            pending,
            causal_parent="receipt:later-candidate",
        )


def test_writer_replay_does_not_double_credit_existing_return():
    signer, actor, genesis, store, trace = _world()
    source = _contract("writer-replay-source", budget=10)
    donor = _contract("writer-replay-donor", budget=6, parent_id=source.id)
    _publish(store, trace, genesis, actor, signer, source, "writer-replay-source")
    _publish(store, trace, genesis, actor, signer, donor, "writer-replay-donor")
    before = tuple(record for _, record in store.scan_runtime_records())
    receipt = _candidate_facts("original-return")
    cancellation = create_terminal_record(
        donor.id,
        "cancelled",
        actor_id=actor.actor_id,
        causal_parents=(receipt.id,),
    )
    returns = project_causal_allowance_returns(
        source,
        (donor,),
        tuple(store.get_all_contracts()),
        before,
        (receipt, cancellation),
        candidate_id="original-return",
        receipt_id=receipt.id,
    )
    committed_facts = (cancellation, *returns)
    head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": "original-return",
            "committed_record_ids": [record.id for record in committed_facts],
        },
        causal_parents=(receipt.id, *(record.id for record in committed_facts)),
    )
    original_batch = (receipt, *committed_facts, head)
    validate_allowance_commit(
        tuple(store.get_all_contracts()),
        before,
        original_batch,
        candidate_id="original-return",
    )
    existing_after = before + original_batch
    new_child = _contract("writer-replay-child", budget=11, parent_id=source.id)
    assert allowance_available(source, existing_after) == 10
    receipt_for_new_child = _candidate_facts("new-child-with-old-return-replay")
    purpose = allowance_purpose(new_child)
    reservation_id = allowance_reservation_id(source.id, new_child.id, purpose)
    repeated_reservation = create_runtime_record(
        "allowance.reserved",
        {
            "amount": new_child.budget,
            "child_contract_id": new_child.id,
            "purpose": purpose,
            "reservation_id": reservation_id,
            "source_contract_id": source.id,
        },
        causal_parents=(receipt_for_new_child.id,),
    )
    new_grant = create_runtime_record(
        "allowance.granted",
        {
            "amount": new_child.budget,
            "contract_id": new_child.id,
            "grant_id": f"grant:{new_child.id}",
            "purpose": purpose,
            "reservation_id": reservation_id,
            "source_contract_id": source.id,
        },
        causal_parents=(repeated_reservation.id,),
    )
    new_declaration = create_runtime_record(
        "contract.declared",
        {
            "candidate_id": "new-child-with-old-return-replay",
            "contract": contract_to_dict(new_child),
        },
        causal_parents=(receipt_for_new_child.id,),
    )
    new_head = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": "new-child-with-old-return-replay",
            "committed_record_ids": [
                new_declaration.id,
                repeated_reservation.id,
                new_grant.id,
            ],
        },
        causal_parents=(
            receipt_for_new_child.id,
            new_declaration.id,
            repeated_reservation.id,
            new_grant.id,
        ),
    )

    with pytest.raises(CausalAllowanceConflict, match="available 10"):
        validate_allowance_commit(
            (*store.get_all_contracts(), new_child),
            existing_after,
            (
                returns[0],
                receipt_for_new_child,
                new_declaration,
                repeated_reservation,
                new_grant,
                new_head,
            ),
        )


def test_terminal_extinguishment_uses_returned_residual_not_old_donor_total():
    signer, actor, genesis, store, trace = _world()
    source = _contract("extinguish-source", budget=7)
    donor = _contract("extinguish-donor", budget=5, parent_id=source.id)
    _publish(store, trace, genesis, actor, signer, source, "ext-source")
    _publish(store, trace, genesis, actor, signer, donor, "ext-donor")
    receipt = _candidate_facts("partial-return")
    cancellation = create_terminal_record(
        donor.id, "cancelled", actor_id=actor.actor_id, causal_parents=(receipt.id,)
    )
    existing = tuple(record for _, record in store.scan_runtime_records())
    returns = project_causal_allowance_returns(
        source,
        (donor,),
        tuple(store.get_all_contracts()),
        existing,
        (receipt, cancellation),
        candidate_id="partial-return",
        receipt_id=receipt.id,
    )
    partial_return = create_runtime_record(
        "allowance.returned",
        {
            **dict(returns[0].payload),
            "amount": 3,
        },
        causal_parents=returns[0].causal_parents,
    )

    extinguished = terminal_allowance_records(
        tuple(store.get_all_contracts()),
        existing,
        (cancellation, partial_return),
    )

    donor_ext = next(
        record
        for record in extinguished
        if record.payload.get("contract_id") == donor.id
    )
    assert donor_ext.payload["amount"] == 2


def test_sqlite_schema19_refuses_legacy_credit_only_returns_and_direct_append(
    tmp_path,
):
    path = tmp_path / "legacy-return.sqlite"
    store = SQLiteStore(path)
    legacy = create_runtime_record(
        "allowance.returned",
        {"source_contract_id": "task:source", "amount": 3},
    )
    store._insert_runtime_record(legacy)
    store._conn.execute("UPDATE schema_version SET version = 18")
    store._conn.commit()
    store.close()

    with pytest.raises(ValueError, match="pre-existing allowance.returned"):
        SQLiteStore(path)
    inspection = sqlite3.connect(path)
    try:
        version = inspection.execute("SELECT version FROM schema_version").fetchone()[0]
        persisted = inspection.execute(
            "SELECT payload_json FROM runtime_records WHERE record_type = 'allowance.returned'"
        ).fetchone()[0]
        assert version == 18
        assert persisted == '{"amount": 3, "source_contract_id": "task:source"}'
    finally:
        inspection.close()

    raw = SQLiteStore(":memory:")
    try:
        with pytest.raises(ValueError, match="Candidate ingress"):
            raw.append_runtime_record(legacy)
    finally:
        raw.close()


def test_sqlite_writer_allowance_gate_checks_return_receipt_and_head(tmp_path):
    store = SQLiteStore(tmp_path / "return-writer-gate.sqlite")
    signer, actor, genesis, store, trace = _world(store)
    try:
        source = _contract("writer-source", budget=7)
        donor = _contract("writer-donor", budget=4, parent_id=source.id)
        _publish(store, trace, genesis, actor, signer, source, "writer-source")
        _publish(store, trace, genesis, actor, signer, donor, "writer-donor")
        successor = _contract("writer-successor", budget=7, parent_id=source.id)
        with store._conn:
            store._insert_contract(successor)
        _, _, _, _, pending = _writer_batch(
            store, source, (donor,), successor, "writer-transfer"
        )

        store._validate_allowance_records(pending, candidate_id="writer-transfer")

        incomplete = tuple(
            record for record in pending if record.record_type != "candidate.committed"
        )
        with pytest.raises(CausalAllowanceConflict, match="Candidate head"):
            store._validate_allowance_records(
                incomplete, candidate_id="writer-transfer"
            )
    finally:
        store.close()


def test_raw_sqlite_append_cannot_mint_causal_allowance(tmp_path):
    store = SQLiteStore(tmp_path / "raw-allowance-append.sqlite")
    signer, actor, genesis, store, trace = _world(store)
    try:
        source = _contract("raw-append-source", budget=5)
        _publish(store, trace, genesis, actor, signer, source, "raw-append-source")
        before = tuple(record for _, record in store.scan_runtime_records())
        forged = create_runtime_record(
            "allowance.granted",
            {
                "amount": 100,
                "contract_id": source.id,
                "grant_id": "grant:synthetic-extra",
                "purpose": "root",
                "source_contract_id": "",
            },
        )

        with pytest.raises(ValueError, match="Candidate ingress commitment"):
            store.append_runtime_record(forged)

        batch_candidate_id = "unsigned-grant-batch"
        receipt = _candidate_facts(batch_candidate_id)
        batch_forgery = create_runtime_record(
            "allowance.granted",
            {
                "amount": 100,
                "contract_id": source.id,
                "grant_id": f"grant:{source.id}",
                "purpose": "root",
                "source_contract_id": "",
            },
        )
        head = create_runtime_record(
            "candidate.committed",
            {
                "candidate_id": batch_candidate_id,
                "committed_record_ids": [batch_forgery.id],
            },
            causal_parents=(receipt.id, batch_forgery.id),
        )
        with pytest.raises(CausalAllowanceConflict, match="declared Contract budget"):
            store.commit_ingress_batch(
                [],
                [],
                runtime_records=(receipt, batch_forgery, head),
                candidate_id=batch_candidate_id,
            )

        assert tuple(record for _, record in store.scan_runtime_records()) == before
        assert (
            allowance_available(
                source, tuple(record for _, record in store.scan_runtime_records())
            )
            == 5
        )
    finally:
        store.close()


def test_schema19_migration_refuses_uncommitted_historical_allowance_grant(
    tmp_path,
):
    path = tmp_path / "uncommitted-allowance-grant.sqlite"
    store = SQLiteStore(path)
    signer, actor, genesis, store, trace = _world(store)
    try:
        source = _contract("historical-grant-source", budget=5)
        _publish(store, trace, genesis, actor, signer, source, "historical-grant")
        fake = create_runtime_record(
            "allowance.granted",
            {
                "amount": 100,
                "contract_id": source.id,
                "grant_id": f"grant:{source.id}",
                "purpose": "root",
                "source_contract_id": "",
            },
            causal_parents=("synthetic-receipt",),
        )
        store._insert_runtime_record(fake)
        store._conn.execute("UPDATE schema_version SET version = 18")
        store._conn.commit()
    finally:
        store.close()

    with pytest.raises(ValueError, match="schema 19 upgrade refuses allowance facts"):
        SQLiteStore(path)
    inspection = sqlite3.connect(path)
    try:
        assert (
            inspection.execute("SELECT version FROM schema_version").fetchone()[0] == 18
        )
        persisted = inspection.execute(
            "SELECT payload_json FROM runtime_records WHERE record_id = ?",
            (fake.id,),
        ).fetchone()[0]
        assert '"amount": 100' in persisted
    finally:
        inspection.close()
