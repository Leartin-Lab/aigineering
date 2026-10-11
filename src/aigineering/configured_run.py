"""Application service for publishing and running a configured local task."""

from __future__ import annotations

import math
from pathlib import Path

from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.application import persistent_store, require_redis
from aigineering.runtime_services import sync_runtime
from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.control_plane import (
    bind_contract_label_assets,
    build_control_plane_contract,
)
from aigineering.local_fleet import (
    FleetHost,
    FleetRunResult,
    LocalCycleResult,
    run_local_cycle,
    run_local_fleet,
)
from aigineering.local_identity import (
    ensure_local_runtime_publishers,
    ensure_local_worker_host,
)
from aigineering.local_publication import commit_local_effects, require_accepted
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.run_config import PreparedConfiguredRun, prepare_configured_run

__all__ = [
    "PreparedConfiguredRun",
    "prepare_configured_run",
    "run_configured",
    "publish_tool_descriptors",
]


def publish_tool_descriptors(store, registry) -> None:
    """Publish descriptors for a configured operator ToolRegistry by Candidate."""
    descriptors = tuple(
        create_tool_descriptor(
            spec.name,
            spec.description,
            spec.input_schema,
            source_uri=f"python:{spec.name}",
            output_schema=spec.output_schema,
            version=spec.version,
            max_output_bytes=spec.max_output_bytes,
        )
        for spec in registry.list_specs()
    )
    effects = tuple(asset_proposal_effect(item) for item in descriptors)
    require_accepted(
        commit_local_effects(
            store,
            effects,
            idempotency_key="tool-registry:"
            + ",".join(sorted(item.id for item in descriptors)),
        )
    )


def _assert_new_run_not_already_present(store, prepared: PreparedConfiguredRun) -> None:
    contract = prepared.contract
    if contract is None:
        return
    same_id = store.get_contract(contract.id)
    same_name = [
        item for item in store.get_all_contracts() if item.name == contract.name
    ]
    output_names = set(contract.outputs)
    historical_outputs = {
        asset.name
        for asset in store.get_all_assets()
        if asset.name in output_names and not asset.tombstoned
    }
    ready_outputs = output_names & historical_outputs
    if same_id is not None or same_name or ready_outputs:
        raise ValueError(
            f"configured root {contract.name!r} already exists or has historical outputs; "
            "resume it with --resume <contract_id>, or use a fresh workspace "
            "for a new run"
        )


def _validate_resume_target(store, target_id: str) -> None:
    if store.get_contract(target_id) is None:
        raise ValueError(f"resume target Contract {target_id!r} does not exist")


def run_configured(
    prepared: PreparedConfiguredRun,
    *,
    timeout: float = 300.0,
    once: bool = False,
) -> FleetRunResult | LocalCycleResult:
    """Publish a new configured root (or resume an existing one), then launch Fleet."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise ValueError("timeout must be a finite positive number")

    if prepared.resume_task_id is not None and not Path(prepared.db_path).is_file():
        raise ValueError(
            f"resume Store does not exist at {prepared.db_path!r}; resume requires an existing root"
        )
    require_redis()
    store = persistent_store(prepared.db_path)
    try:
        if prepared.resume_task_id is not None:
            target_id = prepared.resume_task_id
            _validate_resume_target(store, target_id)
        else:
            target = prepared.contract
            if target is None:
                raise ValueError("new configured run has no prepared root Contract")
            _assert_new_run_not_already_present(store, prepared)
            target_id = _publish_root(store, prepared).id

        ensure_local_runtime_publishers(store)
        hosts = []
        registries = dict(prepared.tool_registries)
        published_registries: set[str] = set()
        for spec, prepared_worker in prepared.workers:
            worker = prepared_worker
            if spec.tool_registry and spec.tool_registry not in published_registries:
                registry = registries[spec.tool_registry]
                publish_tool_descriptors(store, registry)
                published_registries.add(spec.tool_registry)
            host = ensure_local_worker_host(
                store,
                worker,
                effect_capabilities=spec.effect_capabilities,
            )
            hosts.append(FleetHost(host=host, capacity=spec.capacity))
        sync_runtime(store)
        if once:
            return run_local_cycle(
                store,
                tuple(hosts),
                db_path=prepared.db_path,
                target_contract_id=target_id,
                timeout=timeout,
            )
    finally:
        store.close()

    return run_local_fleet(
        prepared.db_path,
        tuple(hosts),
        target_contract_id=target_id,
        timeout=float(timeout),
        poll_interval=prepared.poll_interval,
    )


def _publish_root(store, prepared: PreparedConfiguredRun):
    prepared_contract = prepared.contract
    assert prepared_contract is not None
    ensure_local_runtime_publishers(store)
    effects = tuple(
        asset_proposal_effect(asset)
        for asset in (*prepared.input_assets, *prepared.skill_assets)
    )
    resource_decision = require_accepted(
        commit_local_effects(
            store,
            effects,
            idempotency_key=f"configured-run-resources:{prepared_contract.id}",
        )
    )
    committed_by_name = {asset.name: asset for asset in resource_decision.assets}
    policy = prepared_contract.acceptance_policy
    if prepared.acceptance_evidence_inputs:
        if policy is None:
            raise ValueError("acceptance evidence inputs require an acceptance policy")
        policy = dict(policy)
        try:
            policy["evidence_asset_ids"] = sorted(
                committed_by_name[name].id
                for name in prepared.acceptance_evidence_inputs
            )
        except KeyError as exc:
            raise ValueError(
                f"acceptance evidence input was not published: {exc.args[0]}"
            ) from exc

    contract = build_control_plane_contract(
        name=prepared_contract.name,
        description=prepared_contract.description,
        inputs=prepared_contract.inputs,
        outputs=prepared_contract.outputs,
        activation=prepared_contract.activation,
        budget=prepared_contract.budget,
        labels=prepared_contract.labels,
        tool_scope=prepared_contract.tool_scope,
        worker_capabilities=prepared_contract.worker_capabilities,
        worker_pools=prepared_contract.worker_pools,
        delegation_capabilities=prepared_contract.delegation_capabilities,
        delegation_pools=prepared_contract.delegation_pools,
        sensitive_input_policy=prepared_contract.sensitive_input_policy,
        acceptance_policy=policy,
    )
    for label in contract.labels:
        if label.startswith("plugin:"):
            continue
        if not any(
            asset.name == label and asset.promptable
            for asset in resource_decision.assets
        ):
            raise ValueError(
                f"task label {label!r} did not resolve after Asset publication"
            )

    class _PublishedResources:
        def get_assets_by_name(self, name: str):
            return [asset for asset in resource_decision.assets if asset.name == name]

    contract = bind_contract_label_assets(contract, _PublishedResources())
    validate_contract_commitment(contract)
    committed = require_accepted(
        commit_local_effects(
            store,
            (contract_declaration_effect(contract),),
            idempotency_key=f"configured-run-root:{contract.id}",
        )
    )
    if committed.contract is None:
        raise ValueError("root Contract publication returned no committed Contract")
    return committed.contract
