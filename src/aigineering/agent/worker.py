"""Worker protocol for candidate-producing execution environments."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Protocol, runtime_checkable

from aigineering.core.signing import Signer
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    CandidateProposal,
    GenesisManifest,
    create_candidate_proposal,
)
from aigineering.protocol.actions import (
    ActionParseError,
    parse_action,
    parse_method_action,
)
from aigineering.protocol.effect_builders import claim_bound_output_effects
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
        if envelope.worker_id != self.worker_id:
            raise ValueError("WorkerHost can sign only its own worker envelope")
        if not envelope.raw_output.strip():
            envelope = replace(
                envelope,
                raw_output='/fail {"reason":"worker produced no candidate output"}',
                parsed_action={
                    "type": "fail",
                    "payload": {"reason": "worker produced no candidate output"},
                },
            )
        elif (
            envelope.parsed_action is None
            and not envelope.raw_output.lstrip().startswith("/")
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
                raise WorkerExecutionError(
                    "planning_request_rejected", str(exc)
                ) from exc
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
                raise WorkerExecutionError(
                    "task_delegation_rejected", str(exc)
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
                    )
                except PlanningCompileError as exc:
                    fields = ",".join(exc.fields) or "unknown"
                    raise WorkerExecutionError(
                        f"planning_compile_rejected:{fields}", str(exc)
                    ) from exc
            else:
                effects = claim_bound_output_effects(parsed_envelope)
        binding = CandidateClaimBinding(
            contract_id=envelope.contract_id,
            claim_id=envelope.claim_id,
            claim_epoch=envelope.claim_epoch,
            package_id=envelope.package_id,
        )
        try:
            return create_candidate_proposal(
                domain_id=self.genesis.id,
                actor_id=self.actor_key.actor_id,
                key_id=self.actor_key.key_id,
                effects=effects,
                signer=self.signer,
                idempotency_key=envelope.idempotency_key,
                claim_binding=binding,
                metadata=envelope.usage_metadata,
            )
        except ValueError as exc:
            raise WorkerExecutionError("candidate_encoding_rejected", str(exc)) from exc
