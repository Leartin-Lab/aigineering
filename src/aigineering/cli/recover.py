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
    contract_declaration_effect,
    require_accepted,
)
from aigineering.core.ids import hash_contract_v3
from aigineering.core.method_handlers.recovery import RecoveryMethodHandler
from aigineering.core.method_runtime import MethodRuntime
from aigineering.protocol.types import Candidate, Contract


def _find_recovery_required_contract_ids(store) -> list[str]:
    """Return contract IDs that have ``recovery_required`` trace events,
    deduplicated and in trace-entry order."""
    entries = store.get_by_event_type("recovery_required")
    seen: set[str] = set()
    ids: list[str] = []
    for entry in entries:
        contract_entries = store.get_by_contract(entry.contract_id)
        terminal = {"complete", "failed", "cancelled", "unreachable"}
        resolved = {"recovery_resolved"}
        if any(item.event_type in terminal | resolved for item in contract_entries):
            continue
        if entry.contract_id not in seen:
            seen.add(entry.contract_id)
            ids.append(entry.contract_id)
    return ids


def _cancel_contracts(store, contract_ids: list[str]) -> list[str]:
    """Cancel recovery-required contracts through the recovery method ingress."""
    handler = RecoveryMethodHandler()
    cancelled: list[str] = []
    for cid in contract_ids:
        contract = store.get_contract(cid)
        if contract is None:
            continue
        runtime = MethodRuntime(
            store=store,
            trace=store,
            budget={contract.id: contract.budget},
        )
        candidate = Candidate(
            worker_id="cli:recover",
            raw_output='/recover {"action": "cancel"}',
            parsed_action={"type": "recover", "action": "cancel"},
        )
        if handler.handle_cancel(runtime, contract, candidate):
            cancelled.append(cid)
    return cancelled


def _recreate_contracts(store, contract_ids: list[str]) -> list[dict[str, str]]:
    """Create new contracts with the same parameters as the originals.

    Uses the security-complete v3 identity with ``parent_id`` so the new
    contract gets a deterministic but distinct ID. Publication is an ordinary
    signed ``contract.declare`` Candidate.
    """
    recreated: list[dict[str, str]] = []
    for cid in contract_ids:
        original = store.get_contract(cid)
        if original is None:
            continue

        authority = tuple(
            output
            for output in original.outputs
            if output in original.minting_authority
        )
        policy = (
            dict(original.sensitive_input_policy)
            if original.sensitive_input_policy is not None
            else None
        )
        new_contract = Contract(
            id=hash_contract_v3(
                name=original.name,
                description=original.description,
                inputs=list(original.inputs),
                outputs=list(original.outputs),
                activation=original.activation,
                budget=original.budget,
                tool_scope=list(original.tool_scope),
                labels=list(original.labels),
                worker_capabilities=original.worker_capabilities,
                worker_pools=original.worker_pools,
                origin="recovery",
                parent_id=original.id,
                minting_authority=authority,
                sensitive_input_policy=policy,
            ),
            parent_id=original.id,
            name=original.name,
            description=original.description,
            inputs=original.inputs,
            outputs=original.outputs,
            activation=original.activation,
            budget=original.budget,
            tool_scope=original.tool_scope,
            labels=original.labels,
            worker_capabilities=original.worker_capabilities,
            worker_pools=original.worker_pools,
            origin="recovery",
            minting_authority=authority,
            sensitive_input_policy=original.sensitive_input_policy,
        )
        require_accepted(
            commit_local_effect(
                store,
                contract_declaration_effect(new_contract),
                idempotency_key=f"recover:{original.id}:{new_contract.id}",
            )
        )
        runtime = MethodRuntime(
            store=store,
            trace=store,
            budget={original.id: original.budget},
        )
        runtime.append_trace(
            original.id,
            "recovery_resolved",
            relation_type="recover",
            relation_target=new_contract.id,
            budget_remaining=runtime.resolve_budget(original.id),
        )
        recreated.append({"original_id": cid, "new_id": new_contract.id})
    return recreated


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
    store = _persistent_store()
    contract_ids = _find_recovery_required_contract_ids(store)

    if not contract_ids:
        if json_output:
            _output_json(
                {
                    "recovery_required": [],
                    "cancelled": [],
                    "recreated": [],
                }
            )
        else:
            click.echo("No recovery-required contracts found.")
        return

    cancelled: list[str] = []
    recreated: list[dict[str, str]] = []

    if do_cancel:
        cancelled = _cancel_contracts(store, contract_ids)

    if do_recreate:
        try:
            recreated = _recreate_contracts(store, contract_ids)
        except (LookupError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc

    if json_output:
        _output_json(
            {
                "recovery_required": contract_ids,
                "cancelled": cancelled,
                "recreated": recreated,
            }
        )
        return

    click.echo(f"Recovery-required contracts: {len(contract_ids)}")
    for cid in contract_ids:
        contract = store.get_contract(cid)
        name = contract.name if contract else "?"
        click.echo(f"  {cid}  ({name})")

    if cancelled:
        click.echo(f"\nCancelled: {len(cancelled)}")
        for cid in cancelled:
            click.echo(f"  {cid}")

    if recreated:
        click.echo(f"\nRecreated: {len(recreated)}")
        for r in recreated:
            click.echo(f"  {r['original_id']} -> {r['new_id']}")
