"""Build immutable reducer input snapshots from a StorePort."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aigineering.protocol.runtime_record import RuntimeRecord
from aigineering.core.allowance_publication_index import AllowanceBalanceInput
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    CandidateEffect,
    CandidateProposal,
)
from aigineering.protocol.types import Asset, Contract
from aigineering.core.worker_routing import WorkerRegistration

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol


@dataclass(frozen=True)
class EffectProjectionContext:
    """Immutable fact snapshot required by reference-validating effects."""

    contracts: tuple[Contract, ...] = ()
    assets: tuple[Asset, ...] = ()
    runtime_records: tuple[RuntimeRecord, ...] = ()
    actor_keys: tuple[ActorKey, ...] = ()
    registrations: tuple[WorkerRegistration, ...] = ()
    claim_history_contract_ids: frozenset[str] = frozenset()
    allowance_balance_inputs: tuple[AllowanceBalanceInput, ...] | None = None


@dataclass(frozen=True)
class ContractDeclarationProjectionRequest:
    """Exact immutable read selection for built-in Contract declarations."""

    incoming_contract_ids: tuple[str, ...]
    parent_contract_ids: tuple[str, ...]
    allowance_source_contract_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    candidate_id: str
    include_candidate_replay: bool


@dataclass(frozen=True)
class ContractDeclarationProjectionInputs:
    """Selected existing facts returned by an optional Store reader."""

    contracts: tuple[Contract, ...] = ()
    assets: tuple[Asset, ...] = ()
    runtime_records: tuple[RuntimeRecord, ...] = ()
    allowance_balance_inputs: tuple[AllowanceBalanceInput, ...] | None = None


@dataclass(frozen=True, slots=True)
class AssetProposalProjectionRequest:
    """Exact immutable read selection for built-in Asset proposals."""

    candidate_id: str
    claim_bound_contract_id: str | None
    include_candidate_replay: bool


@dataclass(frozen=True, slots=True)
class AssetProposalProjectionInputs:
    """Selected existing facts returned by an optional Store reader."""

    contracts: tuple[Contract, ...] = ()
    runtime_records: tuple[RuntimeRecord, ...] = ()


_KNOWN_TASK_ID_VERSIONS = frozenset({"3", "4", "5", "6"})
_VERSIONED_TASK_ID = re.compile(r"^task:v([0-9]+):")


def build_contract_declaration_projection_request(
    candidate,
) -> ContractDeclarationProjectionRequest | None:
    """Build a read hint for supported, homogeneous declaration Candidates.

    Parser-domain failures intentionally mean "no narrow request". The caller
    must then load the original full context and let ``reduce_candidate`` retain
    authentication and rejection ownership.
    """
    if not candidate.effects or any(
        effect.effect_type != "contract.declare" for effect in candidate.effects
    ):
        return None

    from aigineering.core.effect_projectors import _contract_from_payload
    from aigineering.core.input_bindings import input_asset_bindings

    incoming_contract_ids: list[str] = []
    parent_contract_ids: list[str] = []
    allowance_source_contract_ids: list[str] = []
    asset_ids: list[str] = []
    for effect in candidate.effects:
        contract = _contract_from_payload(effect.payload)
        version = _VERSIONED_TASK_ID.match(contract.id)
        if version is not None and version.group(1) not in _KNOWN_TASK_ID_VERSIONS:
            return None
        incoming_contract_ids.append(contract.id)
        if contract.parent_id is not None:
            parent_contract_ids.append(contract.parent_id)
            allowance_source_contract_ids.append(contract.parent_id)

        bindings = input_asset_bindings(contract.sensitive_input_policy)
        input_roles = set(contract.inputs)
        for role, bound_ids in bindings.items():
            if role in input_roles:
                asset_ids.extend(bound_ids)
        if contract.id.startswith(("task:v4:", "task:v5:", "task:v6:")):
            asset_ids.extend(contract.context_asset_ids)

    if candidate.claim_binding is not None:
        parent_contract_ids.append(candidate.claim_binding.contract_id)

    def unique(values: list[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(values))

    parents = unique(parent_contract_ids)
    return ContractDeclarationProjectionRequest(
        incoming_contract_ids=unique(incoming_contract_ids),
        parent_contract_ids=parents,
        allowance_source_contract_ids=unique(allowance_source_contract_ids),
        asset_ids=unique(asset_ids),
        candidate_id=candidate.id,
        include_candidate_replay=len(candidate.effects) == 1,
    )


def load_contract_declaration_projection_context(
    store: StoreProtocol,
    candidate,
    *,
    actor_keys: tuple[ActorKey, ...] = (),
) -> EffectProjectionContext:
    """Use an optional narrow Store reader for pure declaration Candidates."""
    try:
        request = build_contract_declaration_projection_request(candidate)
    except (KeyError, TypeError, ValueError):
        request = None
    if request is None:
        return load_effect_projection_context(store, actor_keys=actor_keys)

    reader = getattr(store, "read_contract_declaration_projection_inputs", None)
    if not callable(reader):
        return load_effect_projection_context(store, actor_keys=actor_keys)
    selected = reader(request)
    return EffectProjectionContext(
        contracts=selected.contracts,
        assets=selected.assets,
        runtime_records=selected.runtime_records,
        actor_keys=actor_keys,
        allowance_balance_inputs=getattr(selected, "allowance_balance_inputs", None),
    )


def build_asset_proposal_projection_request(
    candidate: CandidateProposal,
) -> AssetProposalProjectionRequest | None:
    """Build a conservative read hint for a homogeneous proposal Candidate."""
    if type(candidate) is not CandidateProposal:
        return None
    effects = candidate.effects
    if (
        type(effects) is not tuple
        or not effects
        or any(
            type(effect) is not CandidateEffect
            or type(effect.effect_type) is not str
            or effect.effect_type != "asset.propose"
            for effect in effects
        )
    ):
        return None
    if type(candidate.id) is not str or not candidate.id:
        return None

    binding = candidate.claim_binding
    if binding is None:
        parent_id = None
    elif (
        type(binding) is CandidateClaimBinding
        and type(binding.contract_id) is str
        and binding.contract_id
    ):
        parent_id = binding.contract_id
    else:
        return None

    return AssetProposalProjectionRequest(
        candidate_id=candidate.id,
        claim_bound_contract_id=parent_id,
        include_candidate_replay=len(effects) == 1,
    )


def load_asset_proposal_projection_context(
    store: StoreProtocol,
    candidate: CandidateProposal,
    *,
    actor_keys: tuple[ActorKey, ...] = (),
) -> EffectProjectionContext:
    """Use an optional narrow Store reader for pure Asset proposal Candidates."""
    try:
        request = build_asset_proposal_projection_request(candidate)
    except (KeyError, TypeError, ValueError):
        request = None
    if request is None:
        return load_effect_projection_context(store, actor_keys=actor_keys)

    return _load_asset_proposal_projection_context_from_request(
        store, request, actor_keys=actor_keys
    )


def _load_asset_proposal_projection_context_from_request(
    store: StoreProtocol,
    request: AssetProposalProjectionRequest,
    *,
    actor_keys: tuple[ActorKey, ...] = (),
) -> EffectProjectionContext:
    """Load selected Asset inputs without rebuilding the read hint."""
    reader = getattr(store, "read_asset_proposal_projection_inputs", None)
    if not callable(reader):
        return load_effect_projection_context(store, actor_keys=actor_keys)
    selected = reader(request)
    return EffectProjectionContext(
        contracts=selected.contracts,
        runtime_records=selected.runtime_records,
        actor_keys=actor_keys,
    )


def load_candidate_projection_context(
    store: StoreProtocol,
    candidate: CandidateProposal,
    *,
    actor_keys: tuple[ActorKey, ...] = (),
) -> EffectProjectionContext:
    """Choose the narrow Asset reader or the existing declaration path."""
    try:
        request = build_asset_proposal_projection_request(candidate)
    except (KeyError, TypeError, ValueError):
        request = None
    if request is None:
        return load_contract_declaration_projection_context(
            store, candidate, actor_keys=actor_keys
        )
    return _load_asset_proposal_projection_context_from_request(
        store, request, actor_keys=actor_keys
    )


def load_effect_projection_context(
    store: StoreProtocol, *, actor_keys: tuple[ActorKey, ...] = ()
) -> EffectProjectionContext:
    """Read facts once; typed effect projection remains a pure function."""
    return EffectProjectionContext(
        contracts=tuple(store.get_all_contracts()),
        assets=tuple(store.get_all_assets()),
        runtime_records=(
            records := tuple(record for _, record in store.scan_runtime_records())
        ),
        actor_keys=actor_keys,
        registrations=tuple(store.get_worker_registrations()),
        claim_history_contract_ids=frozenset(store.get_claim_history_contract_ids())
        | frozenset(
            str(record.payload.get("contract_id", ""))
            for record in records
            if record.record_type == "claim.granted"
            and record.payload.get("contract_id")
        ),
    )
