"""Engine-as-Worker adapter with an invocation-scoped inner fact domain."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from aigineering.plugins import default_completion_registry
from aigineering.runtime_services import (
    claim_next_application_package,
    order_application_contract_ids,
)
from aigineering.runtime import (
    WorkerInvocationError,
    execute_claimed_package,
    process_task_completions,
    run_runtime_maintenance_step,
)
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
    publish_effect,
)
from aigineering.core.domain import initialize_genesis
from aigineering.core.domain import load_genesis
from aigineering.core.input_bindings import input_asset_bindings
from aigineering.core.ids import (
    canonical_json,
    compute_content_hash,
    hash_asset_content,
    contract_from_fields,
)
from aigineering.core.output_satisfaction import output_asset_is_eligible
from aigineering.core.authority import matched_reserved_prefix
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration, is_compatible
from aigineering.local_identity import (
    ensure_local_runtime_publishers,
    ensure_local_worker_host,
    load_actor_signer,
)
from aigineering.task_views import contract_tree_ids
from aigineering.worker_hosting import authorize_worker_host
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
        inner_workers: tuple[tuple[object, Worker], ...] = (),
        target_domain_id: str | None = None,
        source_domain_id: str | None = None,
        receiver_config_fingerprint: str = "",
        receiver_key_path: Path | None = None,
        receiver_signer_id: str | None = None,
        receiver_tool_registries: tuple[object, ...] = (),
        registration: WorkerRegistration | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self._delegate = delegate
        self._worker_selector = worker_selector
        self.worker_id = worker_id
        self._max_steps = max_steps
        self._uses_ephemeral_inner_domain = inner_store_factory is None
        self._inner_store_factory = inner_store_factory or (
            lambda: SQLiteStore(":memory:")
        )
        self._inner_signer = inner_signer or Ed25519Signer()
        self._bridge_policy = bridge_policy
        self._inner_workers = tuple(inner_workers)
        self._target_domain_id = target_domain_id
        self._source_domain_id = source_domain_id
        self._receiver_config_fingerprint = receiver_config_fingerprint
        self._receiver_key_path = receiver_key_path
        self._receiver_signer_id = receiver_signer_id
        self._receiver_tool_registries = tuple(receiver_tool_registries)
        self._registration = registration or WorkerRegistration(worker_id)
        self.expected_domain_id = source_domain_id
        if target_domain_id is not None and receiver_key_path is None:
            raise ValueError("existing-domain EngineWorker requires receiver key path")

    def invoke(self, contract: Contract, disclosed_assets: list[Asset]) -> Candidate:
        """Return only the outer Contract's declared outputs as a Candidate."""
        return self._invoke(contract, disclosed_assets, claim_binding=None)

    def registration(self) -> WorkerRegistration:
        return self._registration

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
            if claim_binding is not None and claim_binding.contract_id != contract.id:
                return self._failure("outer claim is bound to another Contract")
            if self._target_domain_id is None:
                signer = self._inner_signer
                domain_scope = (
                    uuid4().hex if self._uses_ephemeral_inner_domain else None
                )
                actor_key, genesis, publisher, candidate_publishers = (
                    _initialize_inner_domain(
                        inner, self.worker_id, signer, domain_scope=domain_scope
                    )
                )
            else:
                signer = load_actor_signer(self._receiver_key_path)
                if (
                    self._receiver_signer_id is not None
                    and signer.signer_id != self._receiver_signer_id
                ):
                    return self._failure("receiver key changed after Fleet preparation")
                try:
                    genesis = load_genesis(inner)
                except (LookupError, RuntimeError, ValueError):
                    return self._failure("receiver domain has no valid Genesis")
                if genesis.id != self._target_domain_id:
                    return self._failure("receiver domain Genesis pin changed")
                actor_key = _receiver_root_key(inner, genesis, signer)
                if actor_key is None:
                    return self._failure(
                        "receiver root key is not currently authorized by Genesis"
                    )
                publisher = CandidatePublisher(inner, inner, genesis, actor_key, signer)
                candidate_publishers = ensure_local_runtime_publishers(
                    inner, key_path=self._receiver_key_path
                )
                if self._receiver_tool_registries:
                    from aigineering.configured_run import publish_tool_descriptors

                    for registry in self._receiver_tool_registries:
                        publish_tool_descriptors(
                            inner, registry, key_path=self._receiver_key_path
                        )
            operation_id = _bridge_operation_id(
                contract.id,
                claim_binding,
                self._bridge_policy,
                source_domain_id=self._source_domain_id,
                target_domain_id=genesis.id,
                receiver_config_fingerprint=self._receiver_config_fingerprint,
            )
            input_error, input_id_map = _publish_disclosed_inputs(
                inner, disclosed_assets, genesis, actor_key, signer
            )
            if input_error is not None:
                return self._failure(input_error)
            try:
                inner_contract = _inner_contract(
                    contract,
                    input_assets=disclosed_assets,
                    input_id_map=input_id_map,
                    operation_id=operation_id,
                )
            except ValueError as exc:
                return self._failure(str(exc))
            if not _publish_bridge_operation(
                inner,
                genesis,
                actor_key,
                signer,
                outer=contract,
                inner_contract=inner_contract,
                claim_binding=claim_binding,
                operation_id=operation_id,
                operation_key=_bridge_operation_id(
                    contract.id,
                    claim_binding,
                    self._bridge_policy,
                    source_domain_id=self._source_domain_id,
                    target_domain_id=genesis.id,
                    receiver_config_fingerprint="",
                ),
                bridge_policy=self._bridge_policy,
                source_domain_id=self._source_domain_id,
                target_domain_id=genesis.id,
                receiver_config_fingerprint=self._receiver_config_fingerprint,
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
                self._effective_selector(),
                publisher,
                genesis,
                signer,
                candidate_publishers,
                root_contract_id=inner_contract.id,
                receiver_key_path=self._receiver_key_path,
                inner_workers=self._inner_workers,
                max_steps=self._max_steps,
            ):
                return self._failure("inner worker produced an invalid submission")

            outputs, output_ids = _collect_inner_outputs(
                inner,
                contract.outputs,
                root_contract_id=inner_contract.id,
            )
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
                            "source_domain_id": self._source_domain_id,
                            "target_domain_id": genesis.id,
                            "receiver_config_fingerprint": self._receiver_config_fingerprint,
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

    def _effective_selector(self):
        return self._worker_selector

    def _failure(self, reason: str) -> Candidate:
        # Deliberately undeclared: outer projection records a visible rejection
        # and schedules ordinary recovery instead of silently terminating.
        return Candidate(
            worker_id=self.worker_id,
            raw_output=f"engine_worker_failure: {reason}",
        )


def _initialize_inner_domain(
    store,
    worker_id: str,
    signer: Ed25519Signer,
    *,
    domain_scope: str | None = None,
):
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
    invocation_domain = f"{worker_id}:invocation"
    if domain_scope is not None:
        invocation_domain = f"{invocation_domain}:{domain_scope}"
    genesis = create_genesis_manifest(
        invocation_domain,
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


def _receiver_root_key(store, genesis, signer):
    from aigineering.core.actor_facts import load_effective_actor_keys

    return next(
        (
            key
            for key in load_effective_actor_keys(store, genesis)
            if key.public_key == signer.signer_id
            and not key.revoked
            and {"asset.publish", "contract.publish", "worker.register"}
            <= set(key.capabilities)
        ),
        None,
    )


def _publish_disclosed_inputs(store, assets, genesis, actor_key, signer):
    id_map: dict[str, str] = {}
    for asset in assets:
        if not verify_asset_seal(asset):
            return "outer disclosure contains an invalid asset seal", {}
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
            return "inner domain rejected disclosed input", {}
        if len(decision.assets) != 1:
            return "inner input publication returned no unique Asset", {}
        committed = decision.assets[0]
        if (
            committed.name != asset.name
            or committed.content != asset.content
            or committed.content_type != asset.content_type
            or committed.created_by != actor_key.actor_id
            or committed.signed_by != actor_key.actor_id
            or not verify_asset_seal(committed)
        ):
            return "inner input publication returned an invalid exact Asset", {}
        id_map[asset.id] = committed.id
    return None, id_map


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
    operation_key: str,
    bridge_policy: str,
    source_domain_id: str | None = None,
    target_domain_id: str | None = None,
    receiver_config_fingerprint: str = "",
) -> bool:
    operation_asset = _bridge_asset(
        "bridge_operation",
        operation_key,
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
            "receiver_config_fingerprint": receiver_config_fingerprint,
            "source_domain_id": source_domain_id,
            "target_domain_id": target_domain_id,
        },
    )
    prior = store.get_assets_by_name(operation_asset.name)
    if prior:
        return all(
            verify_asset_seal(asset)
            and asset.content == operation_asset.content
            and asset.created_by == actor_key.actor_id
            and asset.signed_by == actor_key.actor_id
            for asset in prior
        )
    return publish_effect(
        store,
        store,
        genesis,
        actor_key,
        signer,
        asset_proposal_effect(operation_asset),
        idempotency_key=f"bridge-operation:{operation_key}",
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
    root_contract_id: str,
    receiver_key_path: Path | None,
    inner_workers: tuple[tuple[object, Worker], ...],
    max_steps: int,
) -> bool:
    registry = default_completion_registry()
    worker_hosts: dict[str, WorkerHost] = {}
    for _ in range(max_steps):
        run_runtime_maintenance_step(
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
            root_contract_id=root_contract_id,
            receiver_key_path=receiver_key_path,
            inner_workers=inner_workers,
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


def _collect_inner_outputs(store, names, *, root_contract_id: str):
    root = store.get_contract(root_contract_id)
    if root is None:
        return {}, {}
    from aigineering.task_views import project_task_status

    root_status = project_task_status(root, store)
    if root_status.get("status") != "completed":
        return {}, {}
    outputs: dict[str, str] = {}
    output_ids: dict[str, str] = {}
    for name in names:
        matches = [
            asset
            for asset in store.get_assets_by_name(name)
            if output_asset_is_eligible(root, name, asset, store)
        ]
        if len(matches) > 1:
            return {}, {}
        if matches:
            selected = matches[0]
            outputs[name] = selected.content
            output_ids[name] = selected.id
    return outputs, output_ids


def _inner_contract(
    outer: Contract,
    *,
    input_assets: list[Asset],
    input_id_map: dict[str, str],
    operation_id: str,
) -> Contract:
    if outer.minting_authority:
        raise ValueError("receiver Engine Worker does not support minting authority")
    protected = [output for output in outer.outputs if matched_reserved_prefix(output)]
    if protected:
        raise ValueError(
            "receiver Engine Worker does not support protected output names: "
            + ", ".join(sorted(protected))
        )
    source_policy = dict(outer.sensitive_input_policy or {})
    source_bindings = input_asset_bindings(source_policy)
    unknown_binding_roles = set(source_bindings) - set(outer.inputs)
    if unknown_binding_roles:
        raise ValueError(
            "receiver Engine Worker cannot translate bindings for undeclared "
            "input roles: " + ", ".join(sorted(unknown_binding_roles))
        )
    unsupported = set(source_policy) - {"input_asset_bindings"}
    if unsupported:
        raise ValueError(
            "receiver Engine Worker cannot translate sensitive input policy fields: "
            + ", ".join(sorted(unsupported))
        )
    by_id = {asset.id: asset for asset in input_assets}
    bindings: dict[str, tuple[str, ...]] = {}
    for role in outer.inputs:
        source_ids = source_bindings.get(role)
        if source_ids is not None:
            if any(asset_id not in by_id for asset_id in source_ids):
                raise ValueError(
                    f"receiver Engine Worker cannot map exact input binding for {role!r}"
                )
        else:
            source_ids = tuple(asset.id for asset in input_assets if asset.name == role)
            if len(source_ids) != 1:
                raise ValueError(
                    f"receiver Engine Worker requires exactly one disclosed Asset for input {role!r}"
                )
        bindings[role] = tuple(
            sorted(input_id_map[asset_id] for asset_id in source_ids)
        )
    if any(asset_id not in input_id_map for asset_id in outer.context_asset_ids):
        raise ValueError("receiver Engine Worker cannot map exact context Assets")
    context_ids = tuple(
        sorted(input_id_map[asset_id] for asset_id in outer.context_asset_ids)
    )
    policy = {"input_asset_bindings": bindings}
    original = dict(outer.acceptance_policy or {})
    output_shapes = original.get("output_shapes", {})
    acceptance = {
        "mode": "mechanical",
        "output_source": "contract_tree",
        "output_shapes": output_shapes,
    }
    name = f"{outer.name}.engine.{operation_id.rsplit(':', 1)[-1]}"
    return contract_from_fields(
        name=name,
        description=outer.description,
        inputs=list(outer.inputs),
        outputs=list(outer.outputs),
        activation=outer.activation,
        budget=outer.budget,
        tool_scope=list(outer.tool_scope),
        labels=list(outer.labels),
        worker_capabilities=list(outer.worker_capabilities),
        worker_pools=list(outer.worker_pools),
        delegation_capabilities=list(outer.delegation_capabilities),
        delegation_pools=list(outer.delegation_pools),
        origin="engine_worker",
        sensitive_input_policy=policy,
        acceptance_policy=acceptance,
        context_asset_ids=context_ids,
    )


def _claim_inner_work(
    store,
    delegate: Worker,
    selector: Callable[[Contract], Worker] | None,
    publisher: CandidatePublisher,
    genesis,
    root_signer: Ed25519Signer,
    worker_hosts: dict[str, WorkerHost],
    candidate_publishers: CandidatePublisherRegistry,
    *,
    root_contract_id: str,
    receiver_key_path: Path | None,
    inner_workers: tuple[tuple[object, Worker], ...],
):
    contracts = store.get_all_contracts()
    by_id = {contract.id: contract for contract in contracts}
    allowed_ids = contract_tree_ids(contracts, root_contract_id)
    for contract_id in order_application_contract_ids(
        store, tuple(item for item in by_id if item in allowed_ids)
    ):
        contract = by_id[contract_id]
        if selector is not None:
            candidates = ((None, selector(contract)),)
        elif inner_workers:
            candidates = tuple(
                (spec, worker)
                for spec, worker in inner_workers
                if is_compatible(contract, worker.registration())
            )
        else:
            candidates = ((None, delegate),)
        for spec, worker in candidates:
            if worker is None:
                continue
            host = _inner_worker_host(
                worker,
                store,
                worker_hosts,
                effect_capabilities=tuple(getattr(spec, "effect_capabilities", ())),
                receiver_key_path=receiver_key_path,
                outer_publisher=publisher,
                genesis=genesis,
                root_signer=root_signer,
            )
            claimed = claim_next_application_package(
                store,
                worker_id=host.worker_id,
                contract_id=contract.id,
                expected_registration_version=host.registration_version,
                candidate_publishers=candidate_publishers,
                claim_candidate=host.claim_candidate(
                    request_id=f"engine-worker-claim:{uuid4().hex}",
                    contract_id=contract.id,
                ),
            )
            if claimed is not None:
                return claimed, host
    return None


def _inner_worker_host(
    worker: Worker,
    store,
    worker_hosts: dict[str, WorkerHost],
    *,
    effect_capabilities: tuple[str, ...],
    receiver_key_path: Path | None,
    outer_publisher: CandidatePublisher,
    genesis,
    root_signer: Ed25519Signer,
) -> WorkerHost:
    host = worker_hosts.get(worker.worker_id)
    if host is None:
        if receiver_key_path is None:
            seed = hmac.new(
                bytes.fromhex(root_signer.private_key_hex),
                f"aigineering/engine-worker/{worker.worker_id}".encode(),
                hashlib.sha256,
            ).hexdigest()
            worker_signer = Ed25519Signer.from_private_key_hex(seed)
            actor_key = ActorKey(
                worker.worker_id,
                "inner-worker",
                worker_signer.kind,
                worker_signer.signer_id,
                ("worker.submit",),
            )
            host = authorize_worker_host(
                worker, genesis, actor_key, worker_signer, outer_publisher
            )
        else:
            host = ensure_local_worker_host(
                store,
                worker,
                effect_capabilities=effect_capabilities,
                key_path=receiver_key_path,
            )
        worker_hosts[worker.worker_id] = host
    return host


def _bridge_operation_id(
    outer_contract_id: str,
    claim_binding: CandidateClaimBinding | None,
    bridge_policy: str,
    *,
    source_domain_id: str | None = None,
    target_domain_id: str | None = None,
    receiver_config_fingerprint: str = "",
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
                "receiver_config_fingerprint": receiver_config_fingerprint,
                "source_domain_id": source_domain_id,
                "target_domain_id": target_domain_id,
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
