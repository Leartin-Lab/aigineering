"""Engine-as-Worker adapter with an invocation-scoped inner fact domain."""

from __future__ import annotations

import hashlib
import hmac
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
from aigineering.core.ids import (
    canonical_json,
    compute_content_hash,
    hash_asset_content,
    hash_contract_current,
)
from aigineering.core.output_satisfaction import is_business_output
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Asset, Candidate, Contract
from aigineering.worker_hosting import authorize_worker_host

if TYPE_CHECKING:
    from aigineering.agent.worker import Worker, WorkerHost
    from aigineering.core.store import StoreProtocol


class EngineWorker:
    """Run one disclosed Contract in an isolated inner AIG protocol domain."""

    def __init__(
        self,
        delegate: Worker,
        *,
        worker_id: str = "engine_worker:nested",
        max_steps: int = 64,
        worker_selector: Callable[[Contract], Worker] | None = None,
        inner_store_factory: Callable[[], StoreProtocol] | None = None,
        inner_signer: Ed25519Signer | None = None,
        bridge_policy: str = "engine-worker-bridge-v1",
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self._delegate = delegate
        self._worker_selector = worker_selector
        self.worker_id = worker_id
        self._max_steps = max_steps
        self._inner_store_factory = inner_store_factory or (
            lambda: SQLiteStore(":memory:")
        )
        self._inner_signer = inner_signer or Ed25519Signer()
        self._bridge_policy = bridge_policy

    def invoke(self, contract: Contract, disclosed_assets: list[Asset]) -> Candidate:
        """Return only the outer Contract's declared outputs as a Candidate."""
        return self._invoke(contract, disclosed_assets, claim_binding=None)

    def invoke_claimed(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
        claim_binding: CandidateClaimBinding,
    ) -> Candidate:
        """Resume one deterministic outer/inner bridge operation from facts."""
        return self._invoke(contract, disclosed_assets, claim_binding=claim_binding)

    def _invoke(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
        *,
        claim_binding: CandidateClaimBinding | None,
    ) -> Candidate:
        inner = self._inner_store_factory()
        try:
            signer = self._inner_signer
            actor_key, genesis, publisher, candidate_publishers = (
                _initialize_inner_domain(inner, self.worker_id, signer)
            )
            input_error = _publish_disclosed_inputs(
                inner, disclosed_assets, genesis, actor_key, signer
            )
            if input_error is not None:
                return self._failure(input_error)
            inner_contract = _inner_contract(contract)
            operation_id = _bridge_operation_id(
                contract.id, claim_binding, self._bridge_policy
            )
            if not _publish_bridge_operation(
                inner,
                genesis,
                actor_key,
                signer,
                outer=contract,
                inner_contract=inner_contract,
                claim_binding=claim_binding,
                operation_id=operation_id,
                bridge_policy=self._bridge_policy,
            ):
                return self._failure("inner domain rejected bridge operation mapping")
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
            if not _run_inner_steps(
                inner,
                self._delegate,
                self._worker_selector,
                publisher,
                genesis,
                signer,
                candidate_publishers,
                max_steps=self._max_steps,
            ):
                return self._failure("inner worker produced an invalid submission")

            outputs, output_ids = _collect_inner_outputs(inner, contract.outputs)
            if set(outputs) != set(contract.outputs):
                missing = sorted(set(contract.outputs) - set(outputs))
                return self._failure(
                    "inner runtime stopped without declared outputs: "
                    + ", ".join(missing)
                )
            completion = publish_effect(
                inner,
                inner,
                genesis,
                actor_key,
                signer,
                asset_proposal_effect(
                    _bridge_asset(
                        "bridge_completion",
                        operation_id,
                        {
                            "inner_contract_id": inner_contract.id,
                            "operation_id": operation_id,
                            "outer_contract_id": contract.id,
                            "output_asset_ids": output_ids,
                        },
                    )
                ),
                idempotency_key=f"bridge-completion:{operation_id}",
            )
            if not completion.accepted:
                return self._failure("inner domain rejected bridge completion mapping")
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
            close = getattr(inner, "close", None)
            if callable(close):
                close()

    def _failure(self, reason: str) -> Candidate:
        # Deliberately undeclared: outer projection records a visible rejection
        # and schedules ordinary recovery instead of silently terminating.
        return Candidate(
            worker_id=self.worker_id,
            raw_output=f"engine_worker_failure: {reason}",
        )


def _initialize_inner_domain(store, worker_id: str, signer: Ed25519Signer):
    actor_key = ActorKey(
        f"{worker_id}:runtime",
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
        f"{worker_id}:invocation",
        [actor_key],
        "policy:engine-worker-inner-v1",
    )
    initialize_genesis(store, genesis)
    publisher = CandidatePublisher(store, store, genesis, actor_key, signer)
    publishers = CandidatePublisherRegistry(
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
    return actor_key, genesis, publisher, publishers


def _publish_disclosed_inputs(store, assets, genesis, actor_key, signer) -> str | None:
    for asset in assets:
        if not verify_asset_seal(asset):
            return "outer disclosure contains an invalid asset seal"
        decision = publish_effect(
            store,
            store,
            genesis,
            actor_key,
            signer,
            asset_proposal_effect(asset),
            idempotency_key=f"inner-asset:{asset.id}",
        )
        if not decision.accepted:
            return "inner domain rejected disclosed input"
    return None


def _publish_bridge_operation(
    store,
    genesis,
    actor_key,
    signer,
    *,
    outer: Contract,
    inner_contract: Contract,
    claim_binding: CandidateClaimBinding | None,
    operation_id: str,
    bridge_policy: str,
) -> bool:
    operation_asset = _bridge_asset(
        "bridge_operation",
        operation_id,
        {
            "bridge_policy": bridge_policy,
            "inner_contract_id": inner_contract.id,
            "outer_claim": (
                {
                    "claim_epoch": claim_binding.claim_epoch,
                    "claim_id": claim_binding.claim_id,
                    "package_id": claim_binding.package_id,
                }
                if claim_binding is not None
                else None
            ),
            "outer_contract_id": outer.id,
            "operation_id": operation_id,
        },
    )
    return publish_effect(
        store,
        store,
        genesis,
        actor_key,
        signer,
        asset_proposal_effect(operation_asset),
        idempotency_key=f"bridge-operation:{operation_id}",
    ).accepted


def _run_inner_steps(
    store,
    delegate,
    selector,
    publisher,
    genesis,
    signer,
    candidate_publishers,
    *,
    max_steps: int,
) -> bool:
    registry = default_completion_registry()
    worker_hosts: dict[str, tuple[ActorKey, Ed25519Signer]] = {}
    for _ in range(max_steps):
        process_rejected_submissions(store, candidate_publishers=candidate_publishers)
        process_task_completions(
            store, registry, candidate_publishers=candidate_publishers
        )
        selected = _claim_inner_work(
            store,
            delegate,
            selector,
            publisher,
            genesis,
            signer,
            worker_hosts,
            candidate_publishers,
        )
        if selected is None:
            return True
        claimed, worker = selected
        try:
            execute_claimed_package(
                claimed,
                worker,
                store,
                candidate_publishers=candidate_publishers,
            )
        except (ValueError, WorkerInvocationError):
            return False
        process_task_completions(
            store, registry, candidate_publishers=candidate_publishers
        )
    return True


def _collect_inner_outputs(store, names) -> tuple[dict[str, str], dict[str, str]]:
    outputs: dict[str, str] = {}
    output_ids: dict[str, str] = {}
    for name in names:
        matches = [
            asset
            for asset in store.get_assets_by_name(name)
            if is_business_output(asset, name)
        ]
        if matches:
            outputs[name] = matches[-1].content
            output_ids[name] = matches[-1].id
    return outputs, output_ids


def _inner_contract(outer: Contract) -> Contract:
    policy = (
        dict(outer.sensitive_input_policy)
        if outer.sensitive_input_policy is not None
        else None
    )
    identity = hash_contract_current(
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
        context_asset_ids=outer.context_asset_ids,
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
        context_asset_ids=outer.context_asset_ids,
        origin="engine_worker",
        sensitive_input_policy=outer.sensitive_input_policy,
    )


def _claim_inner_work(
    store,
    delegate: Worker,
    selector: Callable[[Contract], Worker] | None,
    publisher: CandidatePublisher,
    genesis,
    root_signer: Ed25519Signer,
    worker_hosts: dict[str, tuple[ActorKey, Ed25519Signer]],
    candidate_publishers: CandidatePublisherRegistry,
):
    for contract in store.get_all_contracts():
        worker = selector(contract) if selector is not None else delegate
        host = _inner_worker_host(worker, publisher, genesis, root_signer, worker_hosts)
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
    root_signer: Ed25519Signer,
    worker_hosts: dict[str, tuple[ActorKey, Ed25519Signer]],
) -> WorkerHost:
    identity = worker_hosts.get(worker.worker_id)
    if identity is None:
        seed = hmac.new(
            bytes.fromhex(root_signer.private_key_hex),
            f"aigineering/engine-worker/{worker.worker_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        signer = Ed25519Signer.from_private_key_hex(seed)
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


def _bridge_operation_id(
    outer_contract_id: str,
    claim_binding: CandidateClaimBinding | None,
    bridge_policy: str,
) -> str:
    claim = (
        {
            "claim_epoch": claim_binding.claim_epoch,
            "claim_id": claim_binding.claim_id,
            "package_id": claim_binding.package_id,
        }
        if claim_binding is not None
        else None
    )
    return "bridge:v1:" + compute_content_hash(
        canonical_json(
            {
                "bridge_policy": bridge_policy,
                "outer_claim": claim,
                "outer_contract_id": outer_contract_id,
            }
        )
    )


def _bridge_asset(kind: str, operation_id: str, payload: dict[str, object]) -> Asset:
    content = canonical_json(payload)
    name = f"{kind}:{operation_id}"
    return Asset(
        id=hash_asset_content(name, content),
        name=name,
        content=content,
        content_type="application/vnd.aigineering.bridge+json",
        origin="engine_worker",
        trust_tier="configured",
        promptable=False,
    )
