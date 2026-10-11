"""aig worker command group (next, submit, package)."""

from __future__ import annotations

import json
import uuid
from typing import Optional

import click

from aigineering.application import validate_worker_id
from aigineering.cli._common import (
    _output_json,
    _persistent_snapshot,
    _persistent_store,
    _redact_sealed,
)
from aigineering.cli._candidate import commit_local_effects, require_accepted
from aigineering.agent.worker import WorkerExecutionError
from aigineering.runtime import (
    WorkerSubmissionCommitError,
    _method_context_assets_for,
    submit_worker_proposal,
)
from aigineering.runtime_services import (
    claim_next_application_package,
    sync_runtime,
)
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.disclosure import DisclosurePolicyError, compute_disclosure
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.domain import load_genesis
from aigineering.core.worker_coordination import (
    authenticate_worker_command,
    parse_worker_command_payload,
)
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.local_identity import (
    ensure_local_worker_host,
)
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateProposal,
    candidate_proposal_from_dict,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    worker_registration_effect,
)
from aigineering.protocol.wire import contract_to_dict


@click.group("worker")
def worker() -> None:
    """Operational worker commands for contract execution."""


class _PullOnlyWorker:
    """Identity carrier for `worker next`; it cannot invoke model work."""

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(self.worker_id)

    def invoke(self, contract, disclosed_assets):
        raise WorkerExecutionError(
            "pull_only", "aig worker next returns packages but does not execute them"
        )


def _worker_next_error(message: str, *, json_output: bool) -> None:
    if json_output:
        _output_json({"error": message, "status": "error"})
        raise click.exceptions.Exit(1)
    raise click.ClickException(message)


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
    """Preview package metadata without creating worker-visible disclosure.

    A submittable WorkerPackage is only emitted by ``worker next`` after an
    atomic claim.  This command is an operator view and never exposes asset
    content or a package identity.
    """
    store = _persistent_snapshot()
    try:
        contract = store.get_contract(contract_id)
        if contract is None:
            if json_output:
                _output_json({"error": f"Contract '{contract_id}' not found."})
            else:
                click.echo(f"Contract '{contract_id}' not found.")
            raise click.exceptions.Exit(1)

        scope = compute_disclosure(contract, store)
        method_context_assets = _method_context_assets_for(contract, store)

        def metadata(asset: object) -> dict[str, object]:
            if isinstance(asset, dict):
                return {
                    key: asset[key]
                    for key in (
                        "id",
                        "name",
                        "content_type",
                        "trust_tier",
                        "promptable",
                        "disclosure_view",
                    )
                    if key in asset
                }
            return {
                "id": asset.id,
                "name": asset.name,
                "content_type": asset.content_type,
                "trust_tier": asset.trust_tier,
                "promptable": asset.promptable,
                "disclosure_view": asset.disclosure_view,
            }

        preview = {
            "view": "operator_package_preview",
            "submittable": False,
            "contract_id": contract.id,
            "contract": contract_to_dict(contract),
            "disclosed_assets": [metadata(asset) for asset in scope],
            "method_context_assets": [
                metadata(asset) for asset in method_context_assets
            ],
            "tool_scope": list(contract.tool_scope),
            "budget_remaining": contract.budget,
            "capability_requirements": list(contract.worker_capabilities),
        }

        if json_output:
            _output_json(preview)
            return

        click.echo(json.dumps(preview, sort_keys=True, ensure_ascii=False))
    finally:
        store.close()


@worker.command("registrations")
@click.argument("worker_id")
@click.option("--json", "json_output", is_flag=True, default=False)
def worker_registrations(worker_id: str, json_output: bool) -> None:
    """Show immutable registration history for one Worker identity."""
    store = _persistent_snapshot()
    try:
        history = [
            (revision, record)
            for revision, record in store.scan_runtime_records(
                record_type="worker.registered"
            )
            if record.payload.get("worker_id") == worker_id
        ]
        if not history:
            message = f"Worker {worker_id!r} has no registration history."
            if json_output:
                _output_json(
                    {
                        "ok": False,
                        "error": {"code": "worker_not_registered", "message": message},
                        "revision": store.get_runtime_revision(),
                    }
                )
            else:
                click.echo(message)
            raise click.exceptions.Exit(1)
        current_registration = store.get_worker_registration(worker_id)
        current_revision, current_record = history[-1]

        def record_view(revision, record):
            return {
                "record_id": record.id,
                "revision": revision,
                "recorded_at": record.recorded_at,
                **deep_thaw(record.payload),
            }

        history_view = [record_view(revision, record) for revision, record in history]
        registration_ids = {record.id for _, record in history}
        promotions = [
            record_view(revision, record)
            for revision, record in store.scan_runtime_records(
                record_type="worker.profile.promoted"
            )
            if record.payload.get("worker_id") == worker_id
            and record.payload.get("registration_id") in registration_ids
        ]
        data = {
            "worker_id": worker_id,
            "current": {
                **record_view(current_revision, current_record),
                "active_claims": (
                    current_registration.active_claims
                    if current_registration is not None
                    else 0
                ),
            },
            "history": history_view,
            "promotions": promotions,
        }
        if json_output:
            _output_json(
                {"ok": True, "data": data, "revision": store.get_runtime_revision()}
            )
            return
        click.echo(f"Worker: {worker_id}")
        click.echo(
            f"Current: v{current_record.payload.get('version')} "
            f"profile={current_record.payload.get('profile_id') or '(unset)'} "
            f"record={current_record.id} active_claims="
            f"{data['current']['active_claims']}"
        )
        click.echo("Registration history:")
        for item in history_view:
            click.echo(
                f"  v{item['version']} profile={item['profile_id'] or '(unset)'} "
                f"record={item['record_id']} revision={item['revision']}"
            )
        if promotions:
            click.echo("Profile promotions:")
            for item in promotions:
                click.echo(
                    f"  {item['previous_registration_id']} -> "
                    f"{item['registration_id']} ({item['profile_id']}) "
                    f"record={item['record_id']}"
                )
    finally:
        store.close()


@worker.command("next")
@click.option("--worker-id", default=None, help="Worker identity for claim ownership.")
@click.option(
    "--lease-seconds",
    type=click.IntRange(min=1),
    default=None,
    help="Lease duration in seconds.",
)
@click.option(
    "--contract", "contract_id", default=None, help="Limit the scan to one Contract."
)
@click.option(
    "--candidate",
    "candidate_json",
    default=None,
    help="Inline signed Worker claim Candidate JSON for an externally registered Worker.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def worker_next(
    worker_id: str | None,
    lease_seconds: int | None,
    contract_id: str | None,
    candidate_json: str | None,
    json_output: bool,
) -> None:
    """Derive the next ready contract and return a WorkerPackage.

    A contract is ready when:
      - Its activation expression is satisfied by available assets
      - Budget remaining > 0
      - It is not completed (no complete trace event, outputs not all satisfied)
      - Its projected blockers are empty
    """
    claim_candidate: CandidateProposal | None = None
    if candidate_json is not None:
        try:
            candidate_data = json.loads(candidate_json)
            if not isinstance(candidate_data, dict):
                raise ValueError("CandidateProposal must be a JSON object")
            claim_candidate = candidate_proposal_from_dict(candidate_data)
            payload = parse_worker_command_payload(claim_candidate, "worker.claim")
        except (ValueError, json.JSONDecodeError) as exc:
            _worker_next_error(
                f"Invalid signed claim Candidate: {exc}", json_output=json_output
            )
        context = click.get_current_context()
        if (
            context.get_parameter_source("worker_id")
            is click.core.ParameterSource.COMMANDLINE
            and worker_id != payload["worker_id"]
        ):
            _worker_next_error(
                "--worker-id conflicts with the signed Candidate",
                json_output=json_output,
            )
        if (
            context.get_parameter_source("lease_seconds")
            is click.core.ParameterSource.COMMANDLINE
            and lease_seconds != payload["lease_seconds"]
        ):
            _worker_next_error(
                "--lease-seconds conflicts with the signed Candidate",
                json_output=json_output,
            )
        if (
            context.get_parameter_source("contract_id")
            is click.core.ParameterSource.COMMANDLINE
            and contract_id != payload["contract_id"]
        ):
            _worker_next_error(
                "--contract conflicts with the signed Candidate",
                json_output=json_output,
            )
        worker_id = payload["worker_id"]
        lease_seconds = payload["lease_seconds"]
        contract_id = payload["contract_id"]
    else:
        worker_id = worker_id or "cli-worker"
        lease_seconds = 60 if lease_seconds is None else lease_seconds
    try:
        validate_worker_id(worker_id)
    except ValueError as exc:
        _worker_next_error(str(exc), json_output=json_output)

    store = _persistent_store()
    try:
        if candidate_json is not None:
            try:
                authenticate_worker_command(claim_candidate, "worker.claim", store)
            except (ValueError, LookupError, json.JSONDecodeError) as exc:
                _worker_next_error(
                    f"Invalid signed claim Candidate: {exc}",
                    json_output=json_output,
                )
        try:
            if candidate_json is None:
                worker = _PullOnlyWorker(worker_id)
                host = ensure_local_worker_host(store, worker, pull_only=True)
                claim_candidate = host.claim_candidate(
                    request_id=f"aig-worker-next:{uuid.uuid4().hex}",
                    contract_id=contract_id,
                    lease_seconds=lease_seconds,
                )
            claimed = claim_next_application_package(
                store,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                contract_id=contract_id,
                claim_candidate=claim_candidate,
                expected_registration_version=(
                    host.registration_version if candidate_json is None else None
                ),
            )
        except DisclosurePolicyError as exc:
            sync_runtime(store)
            result = {
                "status": "policy_blocked",
                "contract_id": exc.contract_id,
                "reasons": list(exc.reasons),
            }
            if json_output:
                _output_json(result)
            else:
                click.echo(json.dumps(result, sort_keys=True, ensure_ascii=False))
            return
        except (ValueError, LookupError) as exc:
            _worker_next_error(str(exc), json_output=json_output)
        sync_runtime(store)
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
                        "status": "blocked_capability",
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
                click.echo("Blocked: no eligible registered worker capability.")
            else:
                click.echo("No ready contracts.")
    finally:
        store.close()


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
@click.option("--version", default="1", show_default=True)
@click.option("--key-id", required=True, help="Authorized Candidate signing key ID.")
@click.option(
    "--public-key", required=True, help="Candidate signing public key material."
)
@click.option(
    "--signature-kind", default="ed25519", show_default=True, help="Key algorithm."
)
@click.option(
    "--candidate-capability",
    "candidate_capabilities",
    multiple=True,
    default=("worker.submit",),
    show_default=True,
    help="Candidate effect capability granted to the worker key.",
)
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
    version: str,
    key_id: str,
    public_key: str,
    signature_kind: str,
    candidate_capabilities: tuple[str, ...],
    disabled: bool,
    json_output: bool,
) -> None:
    """Register trusted routing metadata for a stateless execution worker."""
    store = _persistent_store()
    try:
        registration = WorkerRegistration(
            worker_id=worker_id,
            capabilities=capabilities,
            pools=pools,
            profile_id=profile_id,
            capacity=capacity,
            enabled=not disabled,
            version=version,
            actor_id=worker_id,
            key_id=key_id,
        )
        actor_key = ActorKey(
            worker_id,
            key_id,
            signature_kind,
            public_key,
            candidate_capabilities,
        )
        try:
            existing = next(
                (
                    key
                    for key in load_effective_actor_keys(store, load_genesis(store))
                    if (key.actor_id, key.key_id) == (worker_id, key_id)
                ),
                None,
            )
            if existing is not None and existing != actor_key:
                raise ValueError(
                    f"actor key {worker_id}/{key_id} already exists or is revoked"
                )
            effects = (
                (worker_registration_effect(registration),)
                if existing is not None
                else (
                    actor_authorization_effect(actor_key),
                    worker_registration_effect(registration),
                )
            )
            require_accepted(
                commit_local_effects(
                    store,
                    effects,
                    idempotency_key=f"worker:{worker_id}:{version}",
                )
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        payload = {
            "worker_id": registration.worker_id,
            "capabilities": list(registration.capabilities),
            "pools": list(registration.pools),
            "profile_id": registration.profile_id,
            "capacity": registration.capacity,
            "enabled": registration.enabled,
            "version": registration.version,
            "actor_id": registration.actor_id,
            "key_id": registration.key_id,
        }
        if json_output:
            _output_json(payload)
        else:
            click.echo(json.dumps(payload, ensure_ascii=False))
    finally:
        store.close()


@worker.command("submit")
@click.option(
    "--json", "candidate_json", required=True, help="Signed CandidateProposal JSON"
)
@click.option(
    "--idempotency-key",
    default=None,
    help="Idempotency key for deduplication",
)
def worker_submit(candidate_json: str, idempotency_key: Optional[str]) -> None:
    """Submit a signed worker Candidate for projection and commitment.

    CANDIDATE_JSON must be claim-bound and contain either one atomic
    content/definition/assertion output graph or contained ``contract.declare``
    expansion effects. Output is always JSON.
    """
    try:
        data = json.loads(candidate_json)
        if not isinstance(data, dict):
            raise ValueError("CandidateProposal must be a JSON object")
        candidate = candidate_proposal_from_dict(data)
    except (ValueError, json.JSONDecodeError) as e:
        _output_json({"error": f"Invalid CandidateProposal: {e}"})
        raise click.exceptions.Exit(1) from None

    if idempotency_key and idempotency_key != candidate.idempotency_key:
        _output_json(
            {
                "error": "--idempotency-key must match the signed Candidate value",
                "status": "error",
            }
        )
        raise click.exceptions.Exit(1)

    store = _persistent_store()
    try:
        result = submit_worker_proposal(
            candidate,
            store,
            trace_store=store,
        )
    except CandidateCommitRejected as e:
        sync_runtime(store)
        _output_json({"error": str(e), "status": "error"})
        raise click.exceptions.Exit(1) from None
    except (DisclosurePolicyError, TypeError, ValueError) as e:
        sync_runtime(store)
        _output_json({"error": str(e), "status": "error"})
        raise click.exceptions.Exit(1) from None
    except WorkerSubmissionCommitError as e:
        _output_json({"error": str(e), "status": "error"})
        raise click.exceptions.Exit(1) from None
    else:
        sync_runtime(store)
        # Redact sealed config from result
        result = _redact_sealed(result)
        _output_json(result)
    finally:
        store.close()
