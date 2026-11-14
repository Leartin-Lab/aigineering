"""Application service for publishing and running a configured local task."""

from __future__ import annotations

import math
from pathlib import Path
from dataclasses import replace

from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.application import persistent_store, require_redis
from aigineering.runtime_services import sync_runtime
from aigineering.core.input_bindings import input_asset_bindings
from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.control_plane import (
    bind_contract_label_assets,
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
    local_asset_reference,
)
from aigineering.run_config import PreparedConfiguredRun, prepare_configured_run
from aigineering.protocol.identity import (
    canonical_json,
    compute_content_hash,
    contract_from_fields,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.wire import contract_to_dict

__all__ = [
    "PreparedConfiguredRun",
    "prepare_configured_run",
    "run_configured",
    "tool_descriptor_assets",
    "publish_tool_descriptors",
]


def tool_descriptor_assets(registry) -> tuple:
    """Build the exact signed-publication descriptors for a ToolRegistry."""
    return tuple(
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


def publish_tool_descriptors(store, registry, *, key_path: Path | None = None) -> None:
    """Publish descriptors for a configured operator ToolRegistry by Candidate."""
    descriptors = tool_descriptor_assets(registry)
    effects = tuple(asset_proposal_effect(item) for item in descriptors)
    require_accepted(
        commit_local_effects(
            store,
            effects,
            idempotency_key="tool-registry:"
            + ",".join(sorted(item.id for item in descriptors)),
            key_path=key_path,
        )
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
            target_id = _publish_root(store, prepared).id

        ensure_local_runtime_publishers(store)
        hosts = []
        for _reference, registry in prepared.tool_registries:
            publish_tool_descriptors(store, registry)
        for spec, prepared_worker in prepared.workers:
            worker = prepared_worker
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
    resources = (*prepared.input_assets, *prepared.skill_assets)
    if len({asset.name for asset in resources}) != len(resources):
        raise ValueError("prepared input and Skill Asset names must be unique")
    local_resources = tuple(
        replace(asset, id=local_asset_reference(index))
        for index, asset in enumerate(resources)
    )
    policy = dict(prepared_contract.sensitive_input_policy or {})
    bindings = input_asset_bindings(policy)
    for role in prepared_contract.inputs:
        if role in bindings:
            raise ValueError(
                f"sensitive_input_policy.input_asset_bindings[{role!r}] conflicts "
                "with the published configured input Asset"
            )
    bindings.update(
        _prepared_input_bindings(
            prepared_contract.inputs, local_resources[: len(prepared.input_assets)]
        )
    )
    policy["input_asset_bindings"] = bindings
    acceptance = deep_thaw(prepared_contract.acceptance_policy)
    if prepared.acceptance_evidence_inputs:
        if acceptance is None:
            raise ValueError("acceptance evidence inputs require an acceptance policy")
        by_name = {asset.name: asset.id for asset in local_resources}
        try:
            acceptance["evidence_asset_ids"] = sorted(
                by_name[name] for name in prepared.acceptance_evidence_inputs
            )
        except KeyError as exc:
            raise ValueError(
                f"acceptance evidence input was not prepared: {exc.args[0]}"
            ) from exc
    fields = contract_to_dict(prepared_contract)
    fields.pop("id")
    fields.update(sensitive_input_policy=policy, acceptance_policy=acceptance)
    template = contract_from_fields(**fields)
    for label in template.labels:
        if not label.startswith("plugin:") and not any(
            asset.name == label and asset.promptable for asset in local_resources
        ):
            raise ValueError(
                f"task label {label!r} did not resolve to a prepared Asset"
            )

    class _PreparedResources:
        def get_assets_by_name(self, name: str):
            return [asset for asset in local_resources if asset.name == name]

    template = bind_contract_label_assets(template, _PreparedResources())
    validate_contract_commitment(template)
    effects = tuple(asset_proposal_effect(asset) for asset in resources) + (
        contract_declaration_effect(template, bind_local_assets=True),
    )
    intent_hash = compute_content_hash(
        canonical_json([deep_thaw(effect.payload) for effect in effects])
    )
    ensure_local_runtime_publishers(store)
    committed = require_accepted(
        commit_local_effects(
            store, effects, idempotency_key=f"configured-run:{intent_hash}"
        )
    )
    if committed.contract is None:
        raise ValueError("root Contract publication returned no committed Contract")
    return committed.contract


def _prepared_input_bindings(roles, assets):
    """Resolve each configured input role to one prepared resource reference."""
    by_name: dict[str, list[str]] = {}
    role_set = set(roles)
    for asset in assets:
        if asset.name in role_set:
            by_name.setdefault(asset.name, []).append(asset.id)

    bindings: dict[str, tuple[str, ...]] = {}
    for role in roles:
        asset_ids = tuple(sorted(by_name.get(role, ())))
        if len(asset_ids) != 1:
            raise ValueError(
                f"configured input role {role!r} did not resolve to exactly one "
                "prepared Asset"
            )
        bindings[role] = asset_ids
    return bindings
