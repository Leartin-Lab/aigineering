"""Engine-as-Worker adapter with an invocation-scoped inner fact domain."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from aigineering.plugins import default_completion_registry
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    process_rejected_submissions,
    process_task_completions,
)
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
    publish_effect,
)
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.output_satisfaction import is_business_output
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Candidate, Contract
from aigineering.worker_hosting import authorize_worker_host

if TYPE_CHECKING:
    from aigineering.agent.worker import Worker, WorkerHost
    from aigineering.protocol.types import Asset


class EngineWorker:
    """Run one disclosed Contract in an isolated inner AIG protocol domain."""

    def __init__(
        self,
        delegate: Worker,
        *,
        worker_id: str = "engine_worker:nested",
        max_steps: int = 64,
        worker_selector: Callable[[Contract], Worker] | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self._delegate = delegate
        self._worker_selector = worker_selector
        self.worker_id = worker_id
        self._max_steps = max_steps

    def invoke(self, contract: Contract, disclosed_assets: list[Asset]) -> Candidate:
        """Return only the outer Contract's declared outputs as a Candidate."""
        inner = SQLiteStore(":memory:")
        try:
            signer = Ed25519Signer()
            actor_key = ActorKey(
                f"{self.worker_id}:runtime",
                "inner-root",
                signer.kind,
                signer.signer_id,
                (
                    "actor.authorize",
                    "asset.publish",
                    "asset.publish.protected",
                    "contract.publish",
                    "contract.publish.protected",
                    "worker.register",
                ),
            )
            genesis = create_genesis_manifest(
                f"{self.worker_id}:invocation",
                [actor_key],
                "policy:engine-worker-inner-v1",
            )
            initialize_genesis(inner, genesis)
            publisher = CandidatePublisher(inner, inner, genesis, actor_key, signer)
            candidate_publishers = CandidatePublisherRegistry(
                tuple(
                    (plugin_id, publisher)
                    for plugin_id in (
                        "continuation.publish.v1",
                        "fail.report.v1",
                        "planning.expand.v1",
                        "recovery.publish.v1",
                    )
                )
            )
            for asset in disclosed_assets:
                if not verify_asset_seal(asset):
                    return self._failure(
                        "outer disclosure contains an invalid asset seal"
                    )
                decision = publish_effect(
                    inner,
                    inner,
                    genesis,
                    actor_key,
                    signer,
                    asset_proposal_effect(asset),
                    idempotency_key=f"inner-asset:{asset.id}",
                )
                if not decision.accepted:
                    return self._failure("inner domain rejected disclosed input")
            inner_contract = _inner_contract(contract)
            decision = publish_effect(
                inner,
                inner,
                genesis,
                actor_key,
                signer,
                contract_declaration_effect(inner_contract),
                idempotency_key=f"inner-contract:{inner_contract.id}",
            )
            if not decision.accepted:
                return self._failure("inner domain rejected root contract")
            registry = default_completion_registry()
            worker_hosts: dict[str, tuple[ActorKey, Ed25519Signer]] = {}
            for _ in range(self._max_steps):
                process_rejected_submissions(
                    inner, candidate_publishers=candidate_publishers
                )
                process_task_completions(
                    inner, registry, candidate_publishers=candidate_publishers
                )
                selected = _claim_inner_work(
                    inner,
                    self._delegate,
                    self._worker_selector,
                    publisher,
                    genesis,
                    worker_hosts,
                    candidate_publishers,
                )
                if selected is None:
                    break
                claimed, worker = selected
                try:
                    execute_claimed_package(
                        claimed,
                        worker,
                        inner,
                        candidate_publishers=candidate_publishers,
                    )
                except (ValueError, WorkerInvocationError):
                    return self._failure("inner worker produced an invalid submission")
                process_task_completions(
                    inner, registry, candidate_publishers=candidate_publishers
                )

            outputs: dict[str, str] = {}
            for name in contract.outputs:
                matches = [
                    asset
                    for asset in inner.get_assets_by_name(name)
                    if is_business_output(asset, name)
                ]
                if matches:
                    outputs[name] = matches[-1].content
            if set(outputs) != set(contract.outputs):
                missing = sorted(set(contract.outputs) - set(outputs))
                return self._failure(
                    "inner runtime stopped without declared outputs: "
                    + ", ".join(missing)
                )
            return Candidate(
                worker_id=self.worker_id,
                raw_output=json.dumps(
                    {"type": "exec", "outputs": outputs},
                    sort_keys=True,
                    ensure_ascii=False,
                ),
                parsed_action={"type": "exec", "outputs": outputs},
            )
        finally:
            inner.close()

    def _failure(self, reason: str) -> Candidate:
        # Deliberately undeclared: outer projection records a visible rejection
        # and schedules ordinary recovery instead of silently terminating.
        return Candidate(
            worker_id=self.worker_id,
            raw_output=f"engine_worker_failure: {reason}",
        )


def _inner_contract(outer: Contract) -> Contract:
    policy = (
        dict(outer.sensitive_input_policy)
        if outer.sensitive_input_policy is not None
        else None
    )
    identity = hash_contract_v3(
        name=outer.name,
        description=outer.description,
        inputs=list(outer.inputs),
        outputs=list(outer.outputs),
        activation=outer.activation,
        budget=outer.budget,
        tool_scope=list(outer.tool_scope),
        labels=list(outer.labels),
        worker_capabilities=[],
        worker_pools=[],
        origin="engine_worker",
        sensitive_input_policy=policy,
    )
    return Contract(
        id=identity,
        name=outer.name,
        description=outer.description,
        inputs=outer.inputs,
        outputs=outer.outputs,
        activation=outer.activation,
        budget=outer.budget,
        tool_scope=outer.tool_scope,
        labels=outer.labels,
        origin="engine_worker",
        sensitive_input_policy=outer.sensitive_input_policy,
    )


def _claim_inner_work(
    store,
    delegate: Worker,
    selector: Callable[[Contract], Worker] | None,
    publisher: CandidatePublisher,
    genesis,
    worker_hosts: dict[str, tuple[ActorKey, Ed25519Signer]],
    candidate_publishers: CandidatePublisherRegistry,
):
    for contract in store.get_all_contracts():
        worker = selector(contract) if selector is not None else delegate
        host = _inner_worker_host(worker, publisher, genesis, worker_hosts)
        claimed = claim_next_package(
            store,
            worker_id=host.worker_id,
            contract_id=contract.id,
            candidate_publishers=candidate_publishers,
        )
        if claimed is not None:
            return claimed, host
    return None


def _inner_worker_host(
    worker: Worker,
    publisher: CandidatePublisher,
    genesis,
    worker_hosts: dict[str, tuple[ActorKey, Ed25519Signer]],
) -> WorkerHost:
    identity = worker_hosts.get(worker.worker_id)
    if identity is None:
        signer = Ed25519Signer()
        actor_key = ActorKey(
            worker.worker_id,
            "inner-worker",
            signer.kind,
            signer.signer_id,
            ("worker.submit",),
        )
        identity = (actor_key, signer)
        worker_hosts[worker.worker_id] = identity
    actor_key, signer = identity
    return authorize_worker_host(worker, genesis, actor_key, signer, publisher)
