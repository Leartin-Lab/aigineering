"""Compatibility operations for durable ``recovery_required`` trace facts.

The formal runtime derives ordinary invocation/projection recovery directly
from immutable records.  This command remains for databases carrying explicit
``recovery_required`` facts from earlier releases or administrative tooling.
"""

from __future__ import annotations

import click

from aigineering.cli._common import _output_json, _persistent_store
from aigineering.cli._candidate import (
    commit_local_effect,
    commit_local_effects,
    require_accepted,
)
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.domain import load_genesis
from aigineering.core.ids import contract_from_fields
from aigineering.core.publication_history import committed_candidate_decision
from aigineering.protocol.candidate import (
    CandidateProposal,
    candidate_content_id,
    candidate_received_record,
)
from aigineering.protocol.effect_builders import (
    contract_cancellation_effect,
    contract_declaration_effect,
)
from aigineering.runtime_services import sync_runtime


def _recovery_trace_ids(store) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            entry.contract_id for entry in store.get_by_event_type("recovery_required")
        )
    )


def _resolved(store, contract_id: str) -> bool:
    legacy_resolved = {
        "complete",
        "failed",
        "cancelled",
        "unreachable",
        "recovery_resolved",
    }
    return bool(store.read_task_facts(contract_id, ("lifecycle.terminal",))) or any(
        item.event_type in legacy_resolved
        for item in store.get_by_contract(contract_id)
    )


def _recovery_child_ids(store, contract_id: str) -> list[str]:
    return sorted(
        child.id
        for child in store.get_child_contracts(contract_id)
        if child.origin == "recovery"
    )


def _find_recovery_required_contract_ids(store) -> list[str]:
    return [
        cid
        for cid in _recovery_trace_ids(store)
        if not _resolved(store, cid) and not _recovery_child_ids(store, cid)
    ]


def _cancel_contracts(store, contract_ids: list[str]) -> list[str]:
    """Cancel recovery-required contracts through signed Candidate effects."""
    cancelled: list[str] = []
    for cid in contract_ids:
        contract = store.get_contract(cid)
        if contract is None:
            continue
        require_accepted(
            commit_local_effect(
                store,
                contract_cancellation_effect(contract.id, _CANCELLATION_REASON),
                idempotency_key=f"recover-cancel:{contract.id}",
            )
        )
        cancelled.append(cid)
    return cancelled


_CANCELLATION_REASON = "operator cancelled recovery-required contract"


def _replacement(original):
    return contract_from_fields(
        parent_id=original.id,
        name=original.name,
        description=original.description,
        inputs=original.inputs,
        outputs=original.outputs,
        activation=original.activation,
        budget=original.budget,
        tool_scope=original.tool_scope,
        labels=original.labels,
        context_asset_ids=original.context_asset_ids,
        worker_capabilities=original.worker_capabilities,
        worker_pools=original.worker_pools,
        delegation_capabilities=original.delegation_capabilities,
        delegation_pools=original.delegation_pools,
        origin="recovery",
        minting_authority=tuple(
            output
            for output in original.outputs
            if output in original.minting_authority
        ),
        sensitive_input_policy=original.sensitive_input_policy,
        acceptance_policy=original.acceptance_policy,
    )


def _require_source_allowance(store, contract_id):
    declarations = store.read_task_facts(contract_id, ("contract.declared",))
    if declarations and not store.read_task_facts(contract_id, ("allowance.granted",)):
        raise ValueError(f"signed recovery source {contract_id!r} has no causal grant")


def _recreate_contracts(store, contract_ids: list[str]) -> list[dict[str, str]]:
    """Sign an ordinary replacement declaration for each pending source."""
    recreated: list[dict[str, str]] = []
    for cid in contract_ids:
        original = store.get_contract(cid)
        if original is None:
            continue

        _require_source_allowance(store, original.id)
        new_contract = _replacement(original)
        require_accepted(
            commit_local_effect(
                store,
                contract_declaration_effect(new_contract),
                idempotency_key=f"recover:{original.id}:{new_contract.id}",
            )
        )
        recreated.append({"original_id": cid, "new_id": new_contract.id})
    return recreated


def _prior_action(store, candidate_ids, expected, idempotency_key):
    genesis = load_genesis(store)
    for candidate_id in candidate_ids:
        records = store.read_candidate_publication(candidate_id)
        for receipt in records:
            if receipt.record_type != "candidate.received":
                continue
            payload = receipt.payload
            proposal = CandidateProposal(
                id=candidate_id,
                domain_id=genesis.id,
                actor_id=payload["actor_id"],
                key_id=payload["key_id"],
                signature_kind=payload["signature_kind"],
                signature=payload["signature"],
                effects=expected,
                idempotency_key=idempotency_key,
            )
            if candidate_content_id(proposal) != candidate_id:
                continue
            authenticated = candidate_received_record(
                proposal, genesis, actor_keys=load_effective_actor_keys(store, genesis)
            )
            return committed_candidate_decision(authenticated, records)
    return None


def _prior_recovery(store, replacement, expected, idempotency_key):
    """Prove an exact old action before reusing its replacement."""
    existing = store.get_contract(replacement.id)
    if existing is None:
        return None
    candidate_ids = tuple(
        row.payload["candidate_id"]
        for row in store.read_task_facts(replacement.id, ("contract.declared",))
        if row.payload.get("candidate_id")
    )
    prior = _prior_action(store, candidate_ids, expected, idempotency_key)
    if prior is not None and (
        prior.contracts != (replacement,) or existing != replacement
    ):
        raise ValueError("recovery publication differs from its exact replacement")
    return prior


def _prior_cancellation(store, cid, cancel):
    candidate_ids = []
    for record in store.read_task_facts(cid, ("lifecycle.terminal",)):
        if (
            record.payload.get("terminal") != "cancelled"
            or len(record.causal_parents) != 1
        ):
            continue
        receipt = store.get_runtime_record(record.causal_parents[0])
        if receipt is not None and receipt.record_type == "candidate.received":
            candidate_ids.append(receipt.payload["candidate_id"])
    return _prior_action(store, candidate_ids, (cancel,), f"recover-cancel:{cid}")


def _recreate_and_cancel(store):
    recreated = []
    blocked = []
    for cid in _recovery_trace_ids(store):
        original = store.get_contract(cid)
        if original is None:
            continue
        replacement = _replacement(original)
        declare = contract_declaration_effect(replacement)
        cancel = contract_cancellation_effect(cid, _CANCELLATION_REASON)
        combined_key = f"recover-combined:{cid}:{replacement.id}"
        legacy_key = f"recover:{cid}:{replacement.id}"
        with store.read_transaction():
            prior = _prior_recovery(store, replacement, (declare, cancel), combined_key)
            if prior is None:
                legacy = _prior_recovery(store, replacement, (declare,), legacy_key)
                if _resolved(store, cid):
                    prior = (
                        _prior_cancellation(store, cid, cancel)
                        if legacy is not None
                        else None
                    )
                    if prior is None:
                        continue
                elif legacy is not None:
                    effects = (cancel,)
                elif child_ids := _recovery_child_ids(store, cid):
                    blocked.append(
                        {
                            "original_id": cid,
                            "existing_recovery_ids": child_ids,
                            "reason": "missing_exact_recovery_publication",
                        }
                    )
                    continue
                else:
                    _require_source_allowance(store, cid)
                    effects = (declare, cancel)
        if prior is not None:
            if not any(
                row.record_type == "lifecycle.terminal"
                and row.payload.get("contract_id") == cid
                and row.payload.get("terminal") == "cancelled"
                for row in prior.runtime_records
            ):
                raise ValueError("recovery publication lacks its exact cancellation")
            sync_runtime(store, decision=prior)
        else:
            key = combined_key if len(effects) == 2 else f"recover-cancel:{cid}"
            require_accepted(commit_local_effects(store, effects, idempotency_key=key))
        recreated.append({"original_id": cid, "new_id": replacement.id})
    return recreated, blocked


@click.command("recover")
@click.option(
    "--cancel",
    "do_cancel",
    is_flag=True,
    default=False,
    help="Cancel recovery-required contracts.",
)
@click.option(
    "--recreate",
    "do_recreate",
    is_flag=True,
    default=False,
    help="Recreate recovery-required contracts as new replacement contracts.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def recover(
    do_cancel: bool,
    do_recreate: bool,
    json_output: bool,
) -> None:
    """List, cancel, or recreate contracts marked recovery_required.

    By default (no flags) the command lists all contracts that have
    ``recovery_required`` trace events.  Use ``--cancel`` to mark them
    cancelled or ``--recreate`` to create new contracts with the same
    parameters.  The two flags may be combined.
    """
    blocked = []
    with _persistent_store() as store:
        try:
            if do_recreate and do_cancel:
                recreated, blocked = _recreate_and_cancel(store)
                cancelled = [row["original_id"] for row in recreated]
                contract_ids = cancelled + [row["original_id"] for row in blocked]
            else:
                contract_ids = _find_recovery_required_contract_ids(store)
                recreated = (
                    _recreate_contracts(store, contract_ids) if do_recreate else []
                )
                cancelled = _cancel_contracts(store, contract_ids) if do_cancel else []
            names = {
                cid: contract.name if (contract := store.get_contract(cid)) else "?"
                for cid in contract_ids
            }
        except (LookupError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc

    if json_output:
        result = {
            "recovery_required": contract_ids,
            "cancelled": cancelled,
            "recreated": recreated,
        }
        if blocked:
            result["blocked"] = blocked
        _output_json(result)
        return
    if not contract_ids:
        click.echo("No recovery-required contracts found.")
        return

    click.echo(f"Recovery-required contracts: {len(contract_ids)}")
    for cid in contract_ids:
        click.echo(f"  {cid}  ({names[cid]})")

    if cancelled:
        click.echo(f"\nCancelled: {len(cancelled)}")
        for cid in cancelled:
            click.echo(f"  {cid}")

    if recreated:
        click.echo(f"\nRecreated: {len(recreated)}")
        for r in recreated:
            click.echo(f"  {r['original_id']} -> {r['new_id']}")

    if blocked:
        click.echo("\nExisting replacements without an exact recovery publication:")
        for row in blocked:
            click.echo(
                f"  {row['original_id']} -> {', '.join(row['existing_recovery_ids'])}"
            )
