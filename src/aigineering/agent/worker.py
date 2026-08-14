"""Worker protocol for candidate-producing execution environments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
from typing import Protocol, runtime_checkable

from aigineering.core.signing import Signer
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    CandidateEffect,
    CandidateProposal,
    GenesisManifest,
    WORKER_RAW_OUTPUT_METADATA_KEY,
    create_candidate_proposal,
)
from aigineering.protocol.actions import (
    ActionParseError,
    action_from_dict,
    parse_action,
    parse_method_action,
)
from aigineering.protocol.effect_builders import (
    asset_attestation_effect,
    claim_bound_graph_output_effects,
)
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Asset, Candidate, Contract


class WorkerExecutionError(ValueError):
    """Expected, safely reportable failure before Candidate production."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@runtime_checkable
class Worker(Protocol):
    """Execution environment that returns candidates, never committed facts."""

    worker_id: str

    def invoke(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
    ) -> Candidate: ...


@dataclass(frozen=True)
class WorkerHost:
    """Bind an execution adapter to one authenticated Candidate actor key."""

    worker: Worker
    genesis: GenesisManifest
    actor_key: ActorKey
    signer: Signer

    def __post_init__(self) -> None:
        if self.actor_key.actor_id != self.worker.worker_id:
            raise ValueError("WorkerHost actor_id must equal worker.worker_id")
        if (
            self.actor_key.kind != self.signer.kind
            or self.actor_key.public_key != self.signer.signer_id
        ):
            raise ValueError("WorkerHost signer does not match actor key")
        if "worker.submit" not in self.actor_key.capabilities:
            raise ValueError("WorkerHost actor key requires 'worker.submit'")

    @property
    def worker_id(self) -> str:
        return self.actor_key.actor_id

    def invoke(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
        *,
        claim_binding: CandidateClaimBinding | None = None,
    ) -> Candidate:
        contextual_invoke = getattr(self.worker, "invoke_claimed", None)
        candidate = (
            contextual_invoke(contract, disclosed_assets, claim_binding)
            if claim_binding is not None and callable(contextual_invoke)
            else self.worker.invoke(contract, disclosed_assets)
        )
        if candidate.worker_id != self.worker_id:
            raise WorkerExecutionError(
                "identity_mismatch",
                "worker output identity does not match its WorkerHost actor",
            )
        return candidate

    def sign_envelope(
        self,
        envelope: CandidateEnvelope,
        *,
        contract: Contract | None = None,
        disclosed_assets: tuple[Asset, ...] = (),
        allowance: int | None = None,
    ) -> CandidateProposal:
        return compile_worker_envelope(
            envelope,
            domain_id=self.genesis.id,
            actor_key=self.actor_key,
            signer=self.signer,
            contract=contract,
            disclosed_assets=disclosed_assets,
            allowance=allowance,
        )


def compile_worker_envelope(
    envelope: CandidateEnvelope,
    *,
    domain_id: str,
    actor_key: ActorKey,
    signer: Signer,
    contract: Contract | None = None,
    disclosed_assets: tuple[Asset, ...] = (),
    allowance: int | None = None,
) -> CandidateProposal:
    """Compile one harness action through the canonical Worker effect boundary."""
    if envelope.worker_id != actor_key.actor_id:
        raise ValueError("worker envelope actor does not match its signing key")
    if signer.kind != actor_key.kind or signer.signer_id != actor_key.public_key:
        raise ValueError("worker signer does not match its authorized actor key")
    if "worker.submit" not in actor_key.capabilities:
        raise ValueError("worker actor key requires 'worker.submit'")
    if not envelope.raw_output.strip():
        envelope = replace(
            envelope,
            raw_output='/fail {"reason":"worker produced no candidate output"}',
            parsed_action={
                "type": "fail",
                "payload": {"reason": "worker produced no candidate output"},
            },
        )
    elif envelope.parsed_action is None and not envelope.raw_output.lstrip().startswith(
        "/"
    ):
        outputs: dict[str, str] = {}
        for line in envelope.raw_output.splitlines():
            name, separator, content = line.partition(":")
            if separator and name.strip() and content.strip():
                outputs[name.strip()] = content.strip()
        envelope = replace(
            envelope,
            raw_output="/exec "
            + json.dumps({"outputs": outputs}, sort_keys=True, ensure_ascii=False),
            parsed_action={"type": "exec", "outputs": outputs},
        )
    envelope_candidate = Candidate(
        worker_id=envelope.worker_id,
        raw_output=envelope.raw_output,
        parsed_action=envelope.parsed_action,
        metadata=envelope.usage_metadata,
    )
    action = parse_method_action(envelope_candidate)
    direct_action = None
    if action is None:
        try:
            direct_action = (
                action_from_dict(envelope.parsed_action)
                if envelope.parsed_action is not None
                else parse_action(envelope.raw_output)
            )
        except ActionParseError:
            direct_action = None
    if action is not None and action.type in {"plan", "replan"}:
        if contract is None or contract.id != envelope.contract_id:
            raise ValueError("staged planning requires the claimed Contract")
        from aigineering.plugins import (
            PluginRequest,
            StagedPlanningPlugin,
            StagedReplanningPlugin,
        )

        plugin = (
            StagedPlanningPlugin()
            if action.type == "plan"
            else StagedReplanningPlugin()
        )
        try:
            effects = plugin.propose(
                PluginRequest(
                    parent=contract,
                    assets=tuple(disclosed_assets),
                    allowed_input_names=frozenset(contract.inputs),
                    allowance=contract.budget if allowance is None else allowance,
                    parameters=action.payload,
                )
            ).effects
        except ValueError as exc:
            raise WorkerExecutionError("planning_request_rejected", str(exc)) from exc
    elif action is not None:
        from aigineering.plugins import TaskDelegationPlugin

        if contract is None or contract.id != envelope.contract_id:
            raise ValueError("task expansion requires the claimed Contract")
        try:
            effects = (
                TaskDelegationPlugin()
                .propose_claimed(
                    contract,
                    action,
                    allowance=contract.budget if allowance is None else allowance,
                )
                .effects
            )
        except ValueError as exc:
            raise WorkerExecutionError("task_delegation_rejected", str(exc)) from exc
    elif direct_action is not None and direct_action.type == "attest":
        if contract is None or contract.id != envelope.contract_id:
            raise ValueError("claim-bound attestation requires the claimed Contract")
        try:
            effects = _compile_attestation_action(
                envelope,
                direct_action.payload,
                contract,
                disclosed_assets,
                domain_id=domain_id,
                actor_key=actor_key,
                signer=signer,
            )
        except ValueError as exc:
            raise WorkerExecutionError(
                "attestation_request_rejected", str(exc)
            ) from exc
    else:
        parsed_envelope = envelope
        if envelope.parsed_action is None:
            try:
                parsed = parse_action(envelope.raw_output)
            except ActionParseError as exc:
                raise WorkerExecutionError("invalid_action", str(exc)) from exc
            parsed_envelope = replace(
                envelope,
                parsed_action={
                    "type": parsed.type,
                    "outputs": dict(parsed.outputs),
                },
            )
        if contract is not None and any(
            label in {"plugin:plan.compile", "plugin:replan.compile"}
            for label in contract.labels
        ):
            from aigineering.plugins.planning import (
                PlanningCompileError,
                compile_planning_blueprint,
            )

            try:
                effects = compile_planning_blueprint(
                    contract,
                    (parsed_envelope.parsed_action or {}).get("outputs", {}),
                    allowance=contract.budget if allowance is None else allowance,
                    context_assets=tuple(disclosed_assets),
                )
            except PlanningCompileError as exc:
                fields = ",".join(exc.fields) or "unknown"
                raise WorkerExecutionError(
                    f"planning_compile_rejected:{fields}", str(exc)
                ) from exc
        else:
            if contract is None or contract.id != envelope.contract_id:
                raise ValueError("claim-bound output requires the claimed Contract")
            if contract.origin == "recovery" and contract.tool_scope:
                raise WorkerExecutionError(
                    "tool_observation_required",
                    "tool recovery must request its remaining allowed tool before "
                    "publishing tool-derived outputs",
                )
            effects = _compile_claim_bound_outputs(
                parsed_envelope,
                contract,
                domain_id=domain_id,
                actor_id=actor_key.actor_id,
                key_id=actor_key.key_id,
                signer=signer,
            )
    binding = CandidateClaimBinding(
        contract_id=envelope.contract_id,
        claim_id=envelope.claim_id,
        claim_epoch=envelope.claim_epoch,
        package_id=envelope.package_id,
    )
    try:
        signed_metadata = dict(envelope.usage_metadata or {})
        signed_metadata[WORKER_RAW_OUTPUT_METADATA_KEY] = envelope.raw_output
        return create_candidate_proposal(
            domain_id=domain_id,
            actor_id=actor_key.actor_id,
            key_id=actor_key.key_id,
            effects=effects,
            signer=signer,
            idempotency_key=envelope.idempotency_key,
            claim_binding=binding,
            metadata=signed_metadata,
        )
    except ValueError as exc:
        raise WorkerExecutionError("candidate_encoding_rejected", str(exc)) from exc


def _compile_attestation_action(
    envelope: CandidateEnvelope,
    payload,
    contract: Contract,
    disclosed_assets: tuple[Asset, ...],
    *,
    domain_id: str,
    actor_key: ActorKey,
    signer: Signer,
) -> tuple:
    target_contract_id = payload.get("contract_id")
    output_name = payload.get("output_name")
    asset_id = payload.get("asset_id")
    verdict = payload.get("verdict", "accepted")
    outputs = payload.get("outputs")
    if not all(
        isinstance(value, str) and value
        for value in (target_contract_id, output_name, asset_id)
    ):
        raise ValueError(
            "/attest requires contract_id, output_name, and asset_id strings"
        )
    if verdict not in {"accepted", "rejected"}:
        raise ValueError("/attest verdict must be 'accepted' or 'rejected'")
    if not isinstance(outputs, Mapping) or set(outputs) != set(contract.outputs):
        raise ValueError(
            "/attest outputs must satisfy exactly the verifier task outputs"
        )
    if not all(isinstance(value, str) and value.strip() for value in outputs.values()):
        raise ValueError("/attest output content must be non-empty strings")
    target = next((asset for asset in disclosed_assets if asset.id == asset_id), None)
    if target is None or target.name != output_name:
        raise ValueError("/attest must bind an exact disclosed Asset ID and name")
    rubric_ids = payload.get("rubric_asset_ids", [])
    evidence_ids = payload.get("evidence_asset_ids", [])
    if not isinstance(rubric_ids, list) or not all(
        isinstance(value, str) and value for value in rubric_ids
    ):
        raise ValueError("/attest rubric_asset_ids must be a list of strings")
    if not isinstance(evidence_ids, list) or not all(
        isinstance(value, str) and value for value in evidence_ids
    ):
        raise ValueError("/attest evidence_asset_ids must be a list of strings")
    output_envelope = replace(
        envelope,
        parsed_action={"type": "exec", "outputs": outputs},
    )
    output_effects = _compile_claim_bound_outputs(
        output_envelope,
        contract,
        domain_id=domain_id,
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
        signer=signer,
    )
    attestation = asset_attestation_effect(
        target_contract_id,
        output_name,
        asset_id,
        policy_id=str(payload.get("policy_id", "")),
        policy_version=str(payload.get("policy_version", "")),
        verdict=verdict,
        rubric_asset_ids=tuple(rubric_ids),
        evidence_asset_ids=tuple(evidence_ids),
        atomic_group=f"output:{contract.id}",
    )
    return (*output_effects, attestation)


def _compile_claim_bound_outputs(
    envelope: CandidateEnvelope,
    contract: Contract,
    *,
    domain_id: str,
    actor_id: str,
    key_id: str,
    signer: Signer,
) -> tuple[CandidateEffect, ...]:
    """Keep ordinary and attested outputs on one canonical graph compiler."""
    return claim_bound_graph_output_effects(
        envelope,
        contract,
        domain_id=domain_id,
        actor_id=actor_id,
        key_id=key_id,
        signer=signer,
    )
