"""Engine-as-Worker adapter with an invocation-scoped inner fact domain."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import nullcontext
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from aigineering.runtime_services import (
    claim_next_application_package,
    order_application_contract_ids,
)
from aigineering.runtime import (
    WorkerInvocationError,
    execute_claimed_package,
    run_runtime_maintenance_step,
)
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
    publish_effect,
)
from aigineering.core.acceptance import validate_asset_publication
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.domain import initialize_genesis
from aigineering.core.domain import load_genesis
from aigineering.core.input_bindings import input_asset_bindings
from aigineering.core.ids import (
    canonical_json,
    compute_content_hash,
    hash_asset_content,
    contract_from_fields,
)
from aigineering.core.output_satisfaction import (
    output_asset_is_eligible,
    selected_output_assets,
)
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.publication_history import committed_candidate_decision
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
    CandidateProposal,
    candidate_received_record,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
    definition_publication_effect,
)
from aigineering.protocol.asset_graph import create_signed_definition
from aigineering.protocol.immutability import deep_thaw
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
            operation_id = _bridge_operation_id(
                contract.id,
                claim_binding,
                self._bridge_policy,
                source_domain_id=self._source_domain_id,
                target_domain_id=genesis.id,
                receiver_config_fingerprint=self._receiver_config_fingerprint,
            )
            completion_name = f"bridge_completion:{operation_id}"
            has_completion = inner.has_asset_named(completion_name)
            if not has_completion and self._target_domain_id is not None:
                candidate_publishers = ensure_local_runtime_publishers(
                    inner, key_path=self._receiver_key_path
                )
                if self._receiver_tool_registries:
                    from aigineering.configured_run import publish_tool_descriptors

                    for registry in self._receiver_tool_registries:
                        publish_tool_descriptors(
                            inner, registry, key_path=self._receiver_key_path
                        )
            try:
                input_id_map = _publish_disclosed_inputs(
                    publisher, disclosed_assets, replay_only=has_completion
                )
                inner_contract = _inner_contract(
                    contract,
                    input_assets=disclosed_assets,
                    input_id_map=input_id_map,
                    operation_id=operation_id,
                )
                output_requirements = _publish_output_definitions(
                    publisher,
                    outer=contract,
                    inner_contract=inner_contract,
                    replay_only=has_completion,
                )
                inner_contract = _inner_contract(
                    contract,
                    input_assets=disclosed_assets,
                    input_id_map=input_id_map,
                    operation_id=operation_id,
                    output_requirements=output_requirements,
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
                replay_only=has_completion,
            ):
                return self._failure("inner domain rejected bridge operation mapping")
            completion_binding = {
                "inner_contract_id": inner_contract.id,
                "operation_id": operation_id,
                "outer_contract_id": contract.id,
                "source_domain_id": self._source_domain_id,
                "target_domain_id": genesis.id,
                "receiver_config_fingerprint": self._receiver_config_fingerprint,
            }
            if has_completion:
                return self._replay_completion(
                    publisher, inner_contract, completion_binding
                )

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

            try:
                selected = _collect_inner_outputs(inner, inner_contract)
                outputs = _bridge_output_content(inner_contract, selected)
                completion = _publish_bridge_completion(
                    publisher, completion_binding, selected
                )
            except _BridgeCompletionExists:
                return self._replay_completion(
                    publisher, inner_contract, completion_binding
                )
            except ValueError as exc:
                return self._failure(str(exc))
            if not completion.accepted:
                return self._failure("inner domain rejected bridge completion mapping")
            return self._success(outputs)
        finally:
            close = getattr(inner, "close", None)
            if callable(close):
                close()

    def _effective_selector(self):
        return self._worker_selector

    def _replay_completion(self, publisher, inner_contract, binding) -> Candidate:
        try:
            return self._success(
                _replay_bridge_completion(publisher, inner_contract, binding)
            )
        except (LookupError, ValueError) as exc:
            return self._failure(str(exc))

    def _success(self, outputs: dict[str, str]) -> Candidate:
        action = {"type": "exec", "outputs": outputs}
        return Candidate(
            worker_id=self.worker_id,
            raw_output=json.dumps(action, sort_keys=True, ensure_ascii=False),
            parsed_action=action,
        )

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


def _publish_disclosed_inputs(publisher, assets, *, replay_only):
    id_map: dict[str, str] = {}
    for asset in assets:
        if not verify_asset_seal(asset):
            raise ValueError("outer disclosure contains an invalid asset seal")
        decision = _bridge_setup_effect(
            publisher,
            asset_proposal_effect(asset),
            idempotency_key=f"inner-asset:{asset.id}",
            replay_only=replay_only,
        )
        if not decision.accepted:
            raise ValueError("inner domain rejected disclosed input")
        if len(decision.assets) != 1:
            raise ValueError("inner input publication returned no unique Asset")
        committed = decision.assets[0]
        if (
            committed.name != asset.name
            or committed.content != asset.content
            or committed.content_type != asset.content_type
            or committed.created_by != publisher.actor_key.actor_id
            or committed.signed_by != publisher.actor_key.actor_id
            or not verify_asset_seal(committed)
        ):
            raise ValueError("inner input publication returned an invalid exact Asset")
        id_map[asset.id] = committed.id
    return id_map


def _bridge_setup_effect(publisher, effect, *, idempotency_key, replay_only):
    if not replay_only:
        return publisher.publish((effect,), idempotency_key=idempotency_key)

    store = publisher.store
    candidate = create_candidate_proposal(
        domain_id=publisher.genesis.id,
        actor_id=publisher.actor_key.actor_id,
        key_id=publisher.actor_key.key_id,
        effects=(effect,),
        signer=publisher.signer,
        idempotency_key=idempotency_key,
    )
    read_transaction = getattr(store, "read_transaction", nullcontext)
    with read_transaction():
        records = store.read_candidate_publication(candidate.id)
        if not records:
            raise ValueError("completed bridge lacks exact prior setup publication")
        receipt = candidate_received_record(
            candidate,
            publisher.genesis,
            actor_keys=load_effective_actor_keys(store, publisher.genesis),
        )
        return committed_candidate_decision(receipt, records)


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
    replay_only: bool = False,
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
    if replay_only:
        return False
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
    worker_hosts: dict[str, WorkerHost] = {}
    for _ in range(max_steps):
        run_runtime_maintenance_step(store, candidate_publishers=candidate_publishers)
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

    return True


def _collect_inner_outputs(store, root: Contract) -> dict[str, Asset]:
    # The inner root has no parent. Its immutable terminal facts can block a
    # new export, while historical failed attempts still permit fulfillment.
    if any(
        record.payload.get("terminal") not in {"complete", "failed"}
        for record in store.read_task_facts(root.id, ("lifecycle.terminal",))
    ):
        return {}
    return selected_output_assets(root, store)


def _bridge_output_content(
    root: Contract, selected: dict[str, Asset]
) -> dict[str, str]:
    missing = sorted(set(root.outputs) - set(selected))
    if missing:
        raise ValueError(
            "inner runtime stopped without declared outputs: " + ", ".join(missing)
        )
    for name, asset in selected.items():
        if (
            not asset.promptable
            or asset.disclosure_view != "original"
            or asset.tombstoned
        ):
            raise ValueError(f"inner output {name!r} has no exportable original Asset")
        if not verify_asset_seal(asset):
            raise ValueError(f"inner output {name!r} has an invalid Asset seal")
    return {name: asset.content for name, asset in selected.items()}


class _BridgeCompletionExists(ValueError):
    """Another invocation committed the operation's export selection."""


def _publish_bridge_completion(publisher, binding, selected):
    operation_id = binding["operation_id"]
    payload = {
        **binding,
        "output_asset_ids": {name: asset.id for name, asset in selected.items()},
    }
    draft = _bridge_asset("bridge_completion", operation_id, payload)
    proposal = create_candidate_proposal(
        domain_id=publisher.genesis.id,
        actor_id=publisher.actor_key.actor_id,
        key_id=publisher.actor_key.key_id,
        effects=(asset_proposal_effect(draft),),
        signer=publisher.signer,
        idempotency_key=f"bridge-completion:{operation_id}",
    )

    def validate_fresh():
        if publisher.store.has_asset_named(draft.name):
            raise _BridgeCompletionExists

    return CandidateCommitter(publisher.store, publisher.trace).commit(
        proposal, publisher.genesis, validate_fresh=validate_fresh
    )


def _replay_bridge_completion(publisher, root: Contract, binding) -> dict[str, str]:
    store = publisher.store
    operation_id = binding["operation_id"]
    read_transaction = getattr(store, "read_transaction", nullcontext)
    with read_transaction():
        matches = store.get_assets_by_name(f"bridge_completion:{operation_id}")
        if len(matches) != 1 or store.get_contract(root.id) != root:
            raise ValueError(
                "inner bridge completion has no unique marker and exact root"
            )
        marker = matches[0]
        try:
            payload = json.loads(marker.content)
        except json.JSONDecodeError as exc:
            raise ValueError("inner bridge completion payload is invalid") from exc
        output_ids = (
            payload.get("output_asset_ids") if isinstance(payload, dict) else None
        )
        if (
            not isinstance(output_ids, dict)
            or set(output_ids) != set(root.outputs)
            or any(
                not isinstance(asset_id, str) or not asset_id
                for asset_id in output_ids.values()
            )
            or marker.content
            != canonical_json({**binding, "output_asset_ids": output_ids})
        ):
            raise ValueError("inner bridge completion bindings or output slots differ")

        _validate_bridge_completion_publication(
            publisher, operation_id, marker, payload
        )
        qualified: dict[str, set[str]] = {}
        if (root.acceptance_policy or {}).get("mode") == "independent":
            for record in store.read_task_facts(root.id, ("output.qualified",)):
                name = str(record.payload.get("output_name", ""))
                asset_id = str(record.payload.get("asset_id", ""))
                qualified.setdefault(name, set()).add(asset_id)

        selected: dict[str, Asset] = {}
        for name, asset_id in output_ids.items():
            asset = store.get_asset(asset_id)
            if (
                asset is None
                or asset.id != asset_id
                or not output_asset_is_eligible(
                    root,
                    name,
                    asset,
                    store,
                    qualified_asset_ids=qualified.get(name, frozenset()),
                )
            ):
                raise ValueError(
                    f"inner bridge completion output {name!r} no longer qualifies"
                )
            selected[name] = asset
        return _bridge_output_content(root, selected)


def _validate_bridge_completion_publication(publisher, operation_id, marker, payload):
    store = publisher.store
    actor_key = publisher.actor_key
    expected = _bridge_asset("bridge_completion", operation_id, payload)
    if (
        marker.created_by != actor_key.actor_id
        or marker.signed_by != actor_key.actor_id
        or marker.content_type != expected.content_type
        or marker.origin != expected.origin
        or marker.promptable
        or marker.tombstoned
        or not verify_asset_seal(marker)
    ):
        raise ValueError("inner bridge completion marker has invalid provenance")
    commits = store.read_asset_commit_facts(marker.id)
    if len(commits) != 1 or len(commits[0].causal_parents) != 1:
        raise ValueError("inner bridge completion marker has no exact publication")
    receipt = store.get_runtime_record(commits[0].causal_parents[0])
    if receipt is None or receipt.record_type != "candidate.received":
        raise ValueError("inner bridge completion marker has no exact receipt")
    candidate_id = str(receipt.payload.get("candidate_id", ""))
    context = EffectProjectionContext(
        runtime_records=store.read_candidate_publication(candidate_id)
    )
    proven_receipt, _head, _member = validate_asset_publication(context, marker)
    candidate = CandidateProposal(
        id=candidate_id,
        domain_id=publisher.genesis.id,
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
        signature_kind=actor_key.kind,
        signature=str(receipt.payload.get("signature", "")),
        effects=(asset_proposal_effect(expected),),
        idempotency_key=f"bridge-completion:{operation_id}",
    )
    authenticated = candidate_received_record(
        candidate,
        publisher.genesis,
        actor_keys=load_effective_actor_keys(store, publisher.genesis),
    )
    if authenticated.id != proven_receipt.id or proven_receipt.id != receipt.id:
        raise ValueError(
            "inner bridge completion receipt differs from its signed marker"
        )


def _inner_contract(
    outer: Contract,
    *,
    input_assets: list[Asset],
    input_id_map: dict[str, str],
    operation_id: str,
    output_requirements: dict | None = None,
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
        "output_shapes": output_shapes,
    }
    if output_requirements:
        acceptance["output_requirements"] = output_requirements
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


def _publish_output_definitions(
    publisher, *, outer, inner_contract, replay_only
) -> dict:
    """Describe required outputs by frozen goal and inputs, not task ownership."""
    policy = outer.acceptance_policy or {}
    requirements = {}
    for output in outer.outputs:
        original = policy.get("output_requirements", {}).get(output, {})
        statement = {
            "goal": outer.description,
            "output": output,
            "inputs": input_asset_bindings(inner_contract.sensitive_input_policy),
            "source_requirement": dict(original),
            "shape": policy.get("output_shapes", {}).get(output),
        }
        description = canonical_json(deep_thaw(statement))
        definition = create_signed_definition(
            domain_id=publisher.genesis.id,
            name=output,
            description=description,
            content_type="text",
            source_kind="engine-output-requirement",
            source_uri=f"goal:v1:{compute_content_hash(description)}",
            actor_id=publisher.actor_key.actor_id,
            key_id=publisher.actor_key.key_id,
            signer=publisher.signer,
        )
        result = _bridge_setup_effect(
            publisher,
            definition_publication_effect(definition),
            idempotency_key=f"inner-output-definition:{definition.id}",
            replay_only=replay_only,
        )
        if not result.accepted:
            raise ValueError("inner domain rejected its output requirement definition")
        selectors = {"definition_id": definition.id}
        if "content_id" in original:
            selectors["content_id"] = original["content_id"]
        requirements[output] = selectors
    return requirements


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
