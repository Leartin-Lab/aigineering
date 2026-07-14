"""aig worker command group (next, submit, package)."""

from __future__ import annotations

import json
from typing import Optional

import click

from aigineering.cli._common import (
    _output_json,
    _persistent_store,
    _redact_sealed,
)
from aigineering.cli.worker_runtime import (
    _method_context_assets_for,
    claim_next_package,
)
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.core.submit import (
    SubmitConflictError,
    submit_candidate,
)
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.wire import asset_to_dict, contract_to_dict


@click.group("worker")
def worker() -> None:
    """Operational worker commands for contract execution."""


@worker.command("package")
@click.option(
    "--contract", "contract_id", required=True, help="Contract ID to build package for"
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
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
    method_context_assets = _method_context_assets_for(contract, store)
    pkg = WorkerPackage(
        contract_id=contract.id,
        contract=contract_to_dict(contract),
        disclosed_assets=tuple(asset_to_dict(a) for a in scope),
        method_context_assets=method_context_assets,
        tool_scope=contract.tool_scope,
        budget_remaining=contract.budget,
        capability_requirements=contract.worker_capabilities,
    )

    if json_output:
        result = json.loads(pkg.to_json())
        _output_json(result)
        return

    click.echo(pkg.to_json())


@worker.command("next")
@click.option(
    "--worker-id", default="cli-worker", help="Worker identity for claim ownership."
)
@click.option(
    "--lease-seconds", default=60, show_default=True, help="Lease duration in seconds."
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def worker_next(worker_id: str, lease_seconds: int, json_output: bool) -> None:
    """Derive the next ready contract and return a WorkerPackage.

    A contract is ready when:
      - Its activation expression is satisfied by available assets
      - Budget remaining > 0
      - It is not completed (no complete trace event, outputs not all satisfied)
      - It is not suspended (no outstanding method_scheduled)
    """
    store = _persistent_store()
    claimed = claim_next_package(
        store,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    if claimed is not None:
        pkg = claimed.package
        if json_output:
            result = json.loads(pkg.to_json())
            _output_json(result)
        else:
            click.echo(pkg.to_json())
        return

    if json_output:
        constrained = [
            contract.id
            for contract in store.get_all_contracts()
            if contract.worker_capabilities or contract.worker_pools
        ]
        if constrained:
            _output_json(
                {
                    "status": "waiting_for_capability",
                    "worker_id": worker_id,
                    "contracts": constrained,
                    "message": "No eligible registered worker can claim constrained work.",
                }
            )
        else:
            _output_json(None)
    else:
        constrained = any(
            contract.worker_capabilities or contract.worker_pools
            for contract in store.get_all_contracts()
        )
        if constrained:
            click.echo("Waiting for an eligible registered worker capability.")
        else:
            click.echo("No ready contracts.")


@worker.command("register")
@click.option("--worker-id", required=True, help="Stable execution-worker identity.")
@click.option(
    "--capability",
    "capabilities",
    multiple=True,
    help="Hard capability offered by this worker.",
)
@click.option(
    "--pool", "pools", multiple=True, help="User-defined worker pool membership."
)
@click.option(
    "--profile",
    "profile_id",
    default="",
    help="Versioned provider compatibility profile.",
)
@click.option("--capacity", default=1, show_default=True, type=click.IntRange(min=1))
@click.option(
    "--disabled", is_flag=True, default=False, help="Register as unavailable."
)
@click.option("--json", "json_output", is_flag=True, default=False)
def worker_register(
    worker_id: str,
    capabilities: tuple[str, ...],
    pools: tuple[str, ...],
    profile_id: str,
    capacity: int,
    disabled: bool,
    json_output: bool,
) -> None:
    """Register trusted routing metadata for a stateless execution worker."""
    store = _persistent_store()
    register = getattr(store, "register_worker", None)
    if register is None:
        raise click.ClickException(
            "Worker registration requires SQLite runtime storage."
        )
    registration = WorkerRegistration(
        worker_id=worker_id,
        capabilities=capabilities,
        pools=pools,
        profile_id=profile_id,
        capacity=capacity,
        enabled=not disabled,
    )
    register(registration)
    payload = {
        "worker_id": registration.worker_id,
        "capabilities": list(registration.capabilities),
        "pools": list(registration.pools),
        "profile_id": registration.profile_id,
        "capacity": registration.capacity,
        "enabled": registration.enabled,
    }
    if json_output:
        _output_json(payload)
    else:
        click.echo(json.dumps(payload, ensure_ascii=False))


@worker.command("submit")
@click.option(
    "--json", "envelope_json", required=True, help="CandidateEnvelope JSON string"
)
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

    ingress = RuntimeIngress(store, store)
    try:
        result = submit_candidate(
            envelope=envelope,
            store=store,
            trace_store=store,
            ingress=ingress,
            idempotency_store=None,
            idempotency_key=idempotency_key or "",
        )
    except SubmitConflictError as e:
        _output_json({"error": str(e), "status": "conflict"})
        return
    except Exception as e:
        _output_json({"error": str(e), "status": "error"})
        return

    # Redact sealed config from result
    result = _redact_sealed(result)
    _output_json(result)
