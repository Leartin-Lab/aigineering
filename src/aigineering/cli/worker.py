"""aig worker command group (next, submit, package)."""

from __future__ import annotations

import json
from typing import Optional

import click

from aigineering.cli._common import (
    _get_idempotency_path,
    _get_trace_dir,
    _output_json,
    _persistent_store,
    _redact_sealed,
)
from aigineering.core.activation import check_activation
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.idempotency_store import IdempotencyStore
from aigineering.core.submit import SubmitConflictError, _all_outputs_satisfied, submit_candidate
from aigineering.core.trace import JsonLTraceStore
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.wire import asset_to_dict, contract_to_dict


@click.group("worker")
def worker() -> None:
    """Operational worker commands for contract execution."""


@worker.command("package")
@click.option("--contract", "contract_id", required=True, help="Contract ID to build package for")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def worker_package(contract_id: str, json_output: bool) -> None:
    """Create a WorkerPackage for a contract from the durable store."""
    store = _persistent_store()
    contract = store.get_contract(contract_id)
    if contract is None:
        if json_output:
            _output_json({"error": f"Contract '{contract_id}' not found."})
        else:
            click.echo(f"Contract '{contract_id}' not found.")
        return

    scope = compute_disclosure(contract, store)
    pkg = WorkerPackage(
        contract_id=contract.id,
        contract=contract_to_dict(contract),
        disclosed_assets=tuple(asset_to_dict(a) for a in scope),
        method_context_assets=(),
        tool_scope=contract.tool_scope,
        budget_remaining=contract.budget,
    )

    if json_output:
        result = json.loads(pkg.to_json())
        _output_json(result)
        return

    click.echo(pkg.to_json())


@worker.command("next")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def worker_next(json_output: bool) -> None:
    """Derive the next ready contract and return a WorkerPackage.

    A contract is ready when:
      - Its activation expression is satisfied by available assets
      - Budget remaining > 0
      - It is not completed (no complete trace event, outputs not all satisfied)
      - It is not suspended (no outstanding method_scheduled)
    """
    store = _persistent_store()
    available_names = {a.name for a in store.get_all_assets()}

    for contract in store.get_all_contracts():
        # ── Activation check ────────────────────────────────────────
        if contract.activation and not check_activation(
            contract.activation, available_names
        ):
            continue

        # ── Trace-based state checks ─────────────────────────────────
        trace_path = _get_trace_dir() / f"worker_{contract.id}.jsonl"
        activation_count = 0
        is_completed = False
        is_suspended = False

        if trace_path.exists():
            trace_store = JsonLTraceStore(str(trace_path))
            for entry in trace_store.get_all():
                if entry.event_type == "projection":
                    activation_count += 1
                elif entry.event_type == "complete":
                    is_completed = True
                elif entry.event_type == "method_scheduled":
                    is_suspended = True
                elif entry.event_type == "method_resumed":
                    is_suspended = False

        if is_completed:
            continue

        if is_suspended:
            continue

        remaining_budget = contract.budget - activation_count
        if remaining_budget <= 0:
            continue

        # ── Outputs-satisfied check ──────────────────────────────────
        if _all_outputs_satisfied(contract, store):
            continue

        # ── Build WorkerPackage ──────────────────────────────────────
        scope = compute_disclosure(contract, store)
        pkg = WorkerPackage(
            contract_id=contract.id,
            contract=contract_to_dict(contract),
            disclosed_assets=tuple(asset_to_dict(a) for a in scope),
            method_context_assets=(),
            tool_scope=contract.tool_scope,
            budget_remaining=remaining_budget,
        )

        if json_output:
            result = json.loads(pkg.to_json())
            _output_json(result)
        else:
            click.echo(pkg.to_json())
        return

    # No ready contracts
    if json_output:
        _output_json(None)
    else:
        click.echo("No ready contracts.")


@worker.command("submit")
@click.option("--json", "envelope_json", required=True, help="CandidateEnvelope JSON string")
@click.option(
    "--idempotency-key",
    default=None,
    help="Idempotency key for deduplication",
)
def worker_submit(envelope_json: str, idempotency_key: Optional[str]) -> None:
    """Submit a candidate envelope for projection and commitment.

    ENVELOPE_JSON must be a valid CandidateEnvelope serialized as JSON.
    Output is always JSON.
    """
    try:
        envelope = CandidateEnvelope.from_json(envelope_json)
    except (ValueError, json.JSONDecodeError) as e:
        _output_json({"error": f"Invalid envelope: {e}"})
        return

    store = _persistent_store()

    if store.get_contract(envelope.contract_id) is None:
        _output_json({"error": f"Contract '{envelope.contract_id}' not found."})
        return

    idem_path = _get_idempotency_path()
    idem = IdempotencyStore(idem_path)

    # Use a per-contract trace file for operational submissions
    trace_dir = _get_trace_dir()
    trace_path = str(trace_dir / f"worker_{envelope.contract_id}.jsonl")
    trace_store = JsonLTraceStore(trace_path)

    try:
        result = submit_candidate(
            envelope=envelope,
            store=store,
            trace_store=trace_store,
            idempotency_store=idem,
            idempotency_key=idempotency_key or "",
        )
    except SubmitConflictError as e:
        _output_json({"error": str(e), "status": "conflict"})
        return

    # Redact sealed config from result
    result = _redact_sealed(result)
    _output_json(result)
