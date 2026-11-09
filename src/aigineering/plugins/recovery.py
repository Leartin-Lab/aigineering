"""Build ordinary bounded recovery tasks from authenticated failure facts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from aigineering.core.ids import contract_from_fields
from aigineering.plugins.task_semantics import system_asset
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Contract

if TYPE_CHECKING:
    from aigineering.core.candidate_publisher import CandidatePublisher
    from aigineering.plugins.recovery_history import RecoveryPublicationProof
    from aigineering.core.store import StoreProtocol
    from aigineering.protocol.candidate import CandidateEffect
    from aigineering.protocol.types import Asset


@dataclass(frozen=True)
class RecoveryDisposition:
    contract: Contract | None
    reason: str = ""
    recovery_contract_id: str | None = None


@dataclass(frozen=True)
class MethodRecoveryIntent:
    recovery: Contract
    context_asset: Asset
    source_contract: Contract
    source_record_ids: tuple[str, ...]
    idempotency_key: str
    causal_parents: tuple[str, ...]


def schedule_projection_recovery(
    store: StoreProtocol,
    publisher: CandidatePublisher | None,
    *,
    failed_contract: Contract,
    candidate_raw: str,
    rejections: list[dict],
    source_record_ids: tuple[str, ...],
    source_binding: dict | None = None,
    ancestor_source_bindings: dict[str, dict] | None = None,
    recovery_snapshot=None,
    include_disposition: bool = False,
) -> Contract | RecoveryDisposition | None:
    """Create a normal recovery task for rejected Worker output."""

    if publisher is None:
        result = RecoveryDisposition(None, "recovery_publisher_unavailable")
        return result if include_disposition else None

    source_method, source_error = _recoverable_tool_method(failed_contract)
    if source_error:
        result = RecoveryDisposition(None, source_error)
        return result if include_disposition else None
    if source_method is not None:
        if source_binding is None:
            result = RecoveryDisposition(None, "source_proof_missing")
            return result if include_disposition else None
        source_record_ids = _normalize_source_record_ids(source_record_ids)
        binding_witnesses = source_binding.get("witness_record_ids")
        if (
            not isinstance(binding_witnesses, (tuple, list))
            or tuple(binding_witnesses) != source_record_ids
        ):
            result = RecoveryDisposition(None, "source_proof_missing")
            return result if include_disposition else None
        from aigineering.plugins.recovery_history import RecoveryHistoryCaptureUnstable

        try:
            snapshot = recovery_snapshot or _capture_recovery_history(store, publisher)
        except RecoveryHistoryCaptureUnstable:
            result = RecoveryDisposition(None, "recovery_history_conflict")
            return result if include_disposition else None
        if snapshot is None:
            result = RecoveryDisposition(None, "recovery_history_unavailable")
            return result if include_disposition else None
        snapshot = _with_task_view_query(snapshot, store)
        if isinstance(snapshot, RecoveryDisposition):
            return snapshot if include_disposition else None
        prepared = prepare_method_recovery_intent(
            snapshot,
            failed_contract=failed_contract,
            source_binding=source_binding,
            ancestor_source_bindings=ancestor_source_bindings,
            candidate_raw=candidate_raw,
            rejections=rejections,
        )
        if isinstance(prepared, RecoveryDisposition):
            if prepared.reason in {
                "recovery_unfunded",
                "recovery_consumer_unavailable",
            }:
                source_result = _original_method_source(snapshot, failed_contract)
                source_contract = (
                    source_result[0]
                    if isinstance(source_result, tuple)
                    else failed_contract
                )
                existing = _find_recovery_for_failure(
                    snapshot,
                    publisher,
                    source_contract,
                    tuple(source_binding["witness_record_ids"]),
                    expected_parent_id=failed_contract.id,
                )
                if existing is not None:
                    prepared = RecoveryDisposition(existing)
            return prepared if include_disposition else None
        if prepared is None:
            result = RecoveryDisposition(None, "unsupported_method_intent")
            return result if include_disposition else None
        result = _schedule_tool_method_recovery(
            store,
            publisher,
            intent=prepared,
            recovery_snapshot=snapshot,
        )
        return result if include_disposition else result.contract

    source_record_ids = _normalize_source_record_ids(source_record_ids)
    funder = _generic_recovery_funder(store, failed_contract)
    if funder is None:
        result = RecoveryDisposition(None, "recovery_unfunded")
        return result if include_disposition else None
    recovery, context_template = _projection_recovery_intent(
        failed_contract, funder, candidate_raw, rejections, source_record_ids
    )
    causal_parents = _causal_parents(failed_contract.id, source_record_ids)
    published = _publish_recovery(
        store,
        publisher,
        recovery=recovery,
        context_template=context_template,
        idempotency_key=f"projection-recovery:{failed_contract.id}",
        causal_parents=causal_parents,
        source_record_ids=source_record_ids,
        recovery_snapshot=recovery_snapshot,
    )
    disposition = RecoveryDisposition(
        published[0] if published is not None else None,
        "" if published is not None else "recovery_publication_rejected",
    )
    return disposition if include_disposition else disposition.contract


def _recoverable_tool_method(contract: Contract) -> tuple[dict | None, str]:
    """Read the admitted task's tool intent without reconstructing its maker."""
    payload = _method_payload(contract)
    method = payload.get("method")
    if method not in {"tool", "parallel_tool_item"}:
        return None, ""
    tool = payload.get("payload")
    if (
        not isinstance(tool, dict)
        or not isinstance(tool.get("name"), str)
        or not tool["name"]
        or not isinstance(tool.get("args"), dict)
    ):
        return None, "unsupported_method_intent"
    if method == "parallel_tool_item" and (
        not isinstance(payload.get("parallel_call_id"), str)
        or not payload["parallel_call_id"]
        or not isinstance(payload.get("parallel_batch"), str)
        or not payload["parallel_batch"]
    ):
        return None, "unsupported_method_intent"
    return payload, ""


def _with_task_view_query(snapshot, store):
    """Attach the existing immutable query reader for the live Store revision."""
    from aigineering.task_view_query import (
        TaskViewQueryMismatch,
        load_task_view_query_context,
    )

    try:
        query = load_task_view_query_context(store)
    except (TaskViewQueryMismatch, TypeError, ValueError):
        return RecoveryDisposition(None, "recovery_history_conflict")
    if query.revision != snapshot.revision:
        return RecoveryDisposition(None, "recovery_history_conflict")
    return replace(snapshot, context=query.effect_projection, query_context=query)


def prepare_method_recovery_intent(
    snapshot,
    *,
    failed_contract: Contract,
    source_binding: dict,
    candidate_raw: str = "",
    rejections: list[dict] | None = None,
    ancestor_source_bindings: dict[str, dict] | None = None,
) -> MethodRecoveryIntent | RecoveryDisposition | None:
    """Compile one funded repair from admitted method and actual failure facts."""
    if (
        not isinstance(source_binding, Mapping)
        or source_binding.get("contract_id") != failed_contract.id
        or source_binding.get("kind")
        not in {
            "worker.invocation_failed",
            "claim.expired",
            "candidate.rejected",
            "projection.decided",
        }
        or not isinstance(source_binding.get("grant_record_id"), str)
        or not isinstance(source_binding.get("witness_record_ids"), (tuple, list))
    ):
        return RecoveryDisposition(None, "source_proof_missing")

    source = _original_method_source(snapshot, failed_contract)
    if isinstance(source, RecoveryDisposition):
        return source
    if source is None:
        return RecoveryDisposition(None, "unsupported_method_intent")
    source_contract, method, source_declaration, source_receipt, source_head = source

    consumer = _waiting_observation_consumer(snapshot, source_contract)
    if consumer is None:
        return RecoveryDisposition(None, "recovery_consumer_unavailable")
    funder = _select_recovery_funder(snapshot, consumer, source_contract)
    if funder is None:
        return RecoveryDisposition(None, "recovery_unfunded")

    source_witnesses = tuple(source_binding["witness_record_ids"])
    evidence = tuple(
        dict.fromkeys(
            (
                *source_witnesses,
                source_contract.id,
                source_declaration.id,
                source_receipt.id,
                source_head.id,
            )
        )
    )
    return _build_method_recovery_intent(
        failed_contract,
        source_contract,
        method,
        funder,
        source_binding,
        evidence,
        candidate_raw=candidate_raw,
        rejections=rejections or [],
    )


def _original_method_source(snapshot, failed_contract: Contract):
    """Follow committed recovery context Assets back to one admitted tool Task."""
    from aigineering.core.publication_history import (
        index_published_records,
        validate_published_member,
    )

    contracts = {contract.id: contract for contract in snapshot.context.contracts}
    rows = snapshot.context.runtime_records
    index = index_published_records(rows)
    current = failed_contract
    visited: set[str] = set()
    while current.id not in visited:
        visited.add(current.id)
        declarations = tuple(
            row
            for row in rows
            if row.record_type == "contract.declared"
            and isinstance(row.payload, Mapping)
            and isinstance(row.payload.get("contract"), Mapping)
            and row.payload["contract"].get("id") == current.id
        )
        if len(declarations) != 1:
            return RecoveryDisposition(None, "method_source_declaration_unavailable")
        declaration = declarations[0]
        try:
            receipt, head = validate_published_member(
                rows,
                declaration,
                expected_candidate=declaration.payload.get("candidate_id"),
                domain_id=snapshot.store_domain_id,
                record_index=index,
            )
        except (TypeError, ValueError):
            return RecoveryDisposition(None, "method_source_publication_unverified")

        recovery_sources: list[str] = []
        for record_id in head.payload.get("committed_record_ids", ()):
            member = index.by_id.get(record_id, ())
            if len(member) != 1 or member[0].record_type != "asset.committed":
                continue
            try:
                validate_published_member(
                    rows,
                    member[0],
                    expected_candidate=declaration.payload.get("candidate_id"),
                    expected_receipt=receipt.id,
                    expected_head=head.id,
                    domain_id=snapshot.store_domain_id,
                    record_index=index,
                )
            except (TypeError, ValueError):
                return RecoveryDisposition(None, "method_source_context_unverified")
            asset_value = member[0].payload.get("asset")
            asset_id = (
                asset_value.get("id") if isinstance(asset_value, Mapping) else None
            )
            asset = next(
                (item for item in snapshot.context.assets if item.id == asset_id), None
            )
            if asset is None or not isinstance(asset.content, str):
                continue
            try:
                body = json.loads(asset.content)
            except (TypeError, json.JSONDecodeError):
                continue
            source_id = body.get("source_contract_id")
            if (
                body.get("failed_contract_id") == current.id
                and isinstance(source_id, str)
                and source_id
            ):
                if source_id not in receipt.causal_parents:
                    return RecoveryDisposition(None, "method_source_history_unverified")
                recovery_sources.append(source_id)
            elif (
                body.get("failed_contract_id")
                and body.get("failed_contract_id") != current.id
            ):
                if body["failed_contract_id"] not in receipt.causal_parents:
                    return RecoveryDisposition(None, "method_source_history_unverified")
                recovery_sources.append(str(body["failed_contract_id"]))
        if recovery_sources:
            if len(set(recovery_sources)) != 1:
                return RecoveryDisposition(None, "method_source_history_ambiguous")
            source = contracts.get(recovery_sources[0])
            if source is None:
                return RecoveryDisposition(None, "method_source_history_unverified")
            current = source
            continue

        method, reason = _recoverable_tool_method(current)
        if reason:
            return RecoveryDisposition(None, reason)
        if method is None:
            return RecoveryDisposition(None, "unsupported_method_intent")
        return current, method, declaration, receipt, head
    return RecoveryDisposition(None, "method_source_history_cycle")


def _waiting_observation_consumer(snapshot, source: Contract) -> Contract | None:
    from aigineering.core.activation import activation_names

    output_names = set(source.outputs)
    if not output_names or output_names & {
        asset.name for asset in snapshot.context.assets
    }:
        return None
    query = snapshot.query_context
    candidates = []
    for contract in snapshot.context.contracts:
        if contract.id == source.id:
            continue
        if not output_names.intersection(
            contract.inputs
        ) and not output_names.intersection(activation_names(contract.activation)):
            continue
        if (
            query is not None
            and query.runtime_projection.contract_view(contract).terminal
        ):
            continue
        candidates.append(contract)
    return min(candidates, key=lambda item: item.id) if candidates else None


def _select_recovery_funder(
    snapshot, consumer: Contract, source: Contract
) -> Contract | None:
    from aigineering.core.causal_allowance import allowance_available

    contracts = {item.id: item for item in snapshot.context.contracts}
    records = snapshot.context.runtime_records
    query = snapshot.query_context
    candidates = [consumer]
    current = consumer
    seen = {current.id}
    while current.parent_id:
        parent = contracts.get(current.parent_id)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        candidates.append(parent)
        current = parent

    for index, candidate in enumerate(candidates):
        if (
            query is not None
            and query.runtime_projection.contract_view(candidate).terminal
        ):
            continue
        available = allowance_available(candidate, records)
        # The waiting consumer reserves one unit for its own declared work.
        if available < (2 if index == 0 else 1):
            continue
        if not _recovery_scope_fits(candidate, source):
            continue
        return candidate
    return None


def _recovery_scope_fits(parent: Contract, source: Contract) -> bool:
    if not set(source.tool_scope) <= set(parent.tool_scope):
        return False
    if not set(source.worker_pools) <= set(parent.worker_pools):
        return False
    if not set(source.delegation_pools) <= set(parent.delegation_pools):
        return False
    parent_capabilities = set(parent.worker_capabilities) | set(
        parent.delegation_capabilities
    )
    allowed_tool_capabilities = {
        "tool-execution",
        "mcp-execution",
        *(f"tool:{name}" for name in source.tool_scope),
    }
    if not set(source.worker_capabilities) <= (
        parent_capabilities | allowed_tool_capabilities
    ):
        return False
    if not set(source.delegation_capabilities) <= parent_capabilities:
        return False
    if not set(source.minting_authority) <= set(parent.minting_authority):
        return False
    if not set(source.context_asset_ids) <= set(parent.context_asset_ids):
        return False
    available_inputs = set(parent.inputs)
    available_inputs.update(
        f"_tool_capability_{name}"
        for name in parent.tool_scope
        if not name.startswith("mcp:")
    )
    available_inputs.update(
        f"_mcp_{name[4:].split('.', 1)[0]}"
        for name in parent.tool_scope
        if name.startswith("mcp:")
    )
    if not set(source.inputs) <= available_inputs:
        return False
    return True


def _build_method_recovery_intent(
    failed_contract: Contract,
    source_contract: Contract,
    source_method: dict,
    funder: Contract,
    source_binding: Mapping[str, object],
    source_evidence: tuple[str, ...],
    *,
    candidate_raw: str,
    rejections: list[dict],
) -> MethodRecoveryIntent:
    """Construct a one-unit recovery child under a real funded Contract."""
    payload = dict(source_method)
    context_name = f"_fail_context_{failed_contract.id}.recovery"
    recovery = contract_from_fields(
        name=f"{source_contract.name}.recover.{failed_contract.id[-10:]}",
        description=json.dumps(payload, sort_keys=True, ensure_ascii=False),
        inputs=tuple(dict.fromkeys((*source_contract.inputs, context_name))),
        outputs=source_contract.outputs,
        activation=(
            f"({source_contract.activation}) AND {context_name}"
            if source_contract.activation.strip()
            else context_name
        ),
        budget=1,
        tool_scope=source_contract.tool_scope,
        labels=source_contract.labels,
        worker_capabilities=source_contract.worker_capabilities,
        worker_pools=source_contract.worker_pools,
        delegation_capabilities=source_contract.delegation_capabilities,
        delegation_pools=source_contract.delegation_pools,
        origin="system",
        parent_id=funder.id,
        minting_authority=tuple(
            output
            for output in source_contract.outputs
            if output in source_contract.minting_authority
        ),
        sensitive_input_policy=(
            dict(source_contract.sensitive_input_policy)
            if source_contract.sensitive_input_policy is not None
            else None
        ),
        acceptance_policy=(
            dict(source_contract.acceptance_policy)
            if source_contract.acceptance_policy is not None
            else None
        ),
        context_asset_ids=source_contract.context_asset_ids,
    )
    stable_binding = {
        key: source_binding[key]
        for key in (
            "kind",
            "contract_id",
            "claim_id",
            "grant_record_id",
            "failure_record_id",
            "release_record_id",
            "source_record_id",
            "witness_record_ids",
        )
        if key in source_binding
    }
    context_asset = system_asset(
        name=context_name,
        content=json.dumps(
            {
                "trigger": "verified_task_failure",
                "failed_contract_id": failed_contract.id,
                "source_contract_id": source_contract.id,
                "source_binding": stable_binding,
                "bad_worker_output": candidate_raw[:4000],
                "rejections": rejections,
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
        created_by=funder.id,
        promptable=True,
    )
    return MethodRecoveryIntent(
        recovery=recovery,
        context_asset=context_asset,
        source_contract=source_contract,
        source_record_ids=source_evidence,
        idempotency_key=f"projection-recovery:{failed_contract.id}",
        causal_parents=tuple(dict.fromkeys((failed_contract.id, *source_evidence))),
    )


def _schedule_tool_method_recovery(
    store: StoreProtocol,
    publisher: CandidatePublisher,
    *,
    intent: MethodRecoveryIntent,
    recovery_snapshot=None,
) -> RecoveryDisposition:
    """Recognize exact prior facts, then publish one ordinary recovery Candidate."""
    from aigineering.plugins.recovery_history import RecoveryHistoryCaptureUnstable

    try:
        snapshot = recovery_snapshot or _capture_recovery_history(store, publisher)
    except RecoveryHistoryCaptureUnstable:
        return RecoveryDisposition(None, "recovery_history_conflict")
    if snapshot is None:
        return RecoveryDisposition(None, "recovery_history_unavailable")
    existing = _find_recovery_for_failure(
        snapshot,
        publisher,
        intent.source_contract,
        tuple(
            record_id
            for record_id in intent.source_record_ids
            if record_id not in {intent.source_contract.id}
        ),
        expected_parent_id=intent.causal_parents[0],
    )
    if existing is not None:
        return RecoveryDisposition(existing)
    decision = publisher.publish(
        _recovery_effects(intent.recovery, intent.context_asset),
        idempotency_key=intent.idempotency_key,
        causal_parents=intent.causal_parents,
    )
    if decision.accepted and len(decision.contracts) == 1:
        return RecoveryDisposition(decision.contracts[0])
    try:
        recaptured = _capture_recovery_history(publisher.store, publisher)
    except RecoveryHistoryCaptureUnstable:
        return RecoveryDisposition(None, "recovery_history_conflict")
    existing = _find_recovery_for_failure(
        recaptured,
        publisher,
        intent.source_contract,
        tuple(
            record_id
            for record_id in intent.source_record_ids
            if record_id not in {intent.source_contract.id}
        ),
        expected_parent_id=intent.causal_parents[0],
    )
    return (
        RecoveryDisposition(existing)
        if existing is not None
        else RecoveryDisposition(None, "recovery_publication_rejected")
    )


def _find_recovery_for_failure(
    snapshot,
    publisher,
    source_contract: Contract,
    source_witness_ids: tuple[str, ...],
    *,
    expected_parent_id: str | None = None,
) -> Contract | None:
    """Read a recovery from its actual declaration, receipt and closed head."""
    from aigineering.core.publication_history import (
        index_published_records,
        read_effect_types,
        validate_published_member,
    )

    rows = snapshot.context.runtime_records
    index = index_published_records(rows)
    matches: list[Contract] = []
    for declaration in rows:
        if declaration.record_type != "contract.declared":
            continue
        raw = declaration.payload.get("contract")
        if not isinstance(raw, Mapping):
            continue
        contract_id = raw.get("id")
        receipt_row = next(
            (
                row
                for row in rows
                if row.record_type == "candidate.received"
                and row.payload.get("candidate_id")
                == declaration.payload.get("candidate_id")
            ),
            None,
        )
        if receipt_row is None:
            continue
        payload = receipt_row.payload
        causal = tuple(receipt_row.causal_parents)
        if (
            payload.get("actor_id") != snapshot.publisher_actor_id
            or len(causal) < 2
            or causal[0] != expected_parent_id
            or not set(source_witness_ids) <= set(causal)
        ):
            continue
        try:
            if read_effect_types(payload) != ("contract.declare", "asset.propose"):
                continue
            _receipt, head = validate_published_member(
                rows,
                declaration,
                expected_candidate=declaration.payload.get("candidate_id"),
                expected_receipt=receipt_row.id,
                domain_id=snapshot.store_domain_id,
                record_index=index,
            )
        except (TypeError, ValueError):
            continue
        committed_ids = set(head.payload.get("committed_record_ids", ()))
        context_match = False
        for record_id in committed_ids:
            members = index.by_id.get(record_id, ())
            if len(members) != 1 or members[0].record_type != "asset.committed":
                continue
            try:
                validate_published_member(
                    rows,
                    members[0],
                    expected_candidate=declaration.payload.get("candidate_id"),
                    expected_receipt=receipt_row.id,
                    expected_head=head.id,
                    domain_id=snapshot.store_domain_id,
                    record_index=index,
                )
            except (TypeError, ValueError):
                continue
            asset_value = members[0].payload.get("asset")
            if not isinstance(asset_value, Mapping):
                continue
            asset = next(
                (
                    item
                    for item in snapshot.context.assets
                    if item.id == asset_value.get("id")
                ),
                None,
            )
            if asset is None:
                continue
            try:
                body = json.loads(asset.content)
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                body.get("failed_contract_id") == expected_parent_id
                and body.get("source_contract_id", expected_parent_id)
                == source_contract.id
            ):
                context_match = True
                break
        if context_match:
            materialized = next(
                (item for item in snapshot.context.contracts if item.id == contract_id),
                None,
            )
            if materialized is not None:
                matches.append(materialized)
    if len(matches) > 1:
        return None
    return matches[0] if matches else None


def _projection_recovery_intent(
    failed_contract: Contract,
    funder: Contract,
    candidate_raw: str,
    rejections: list[dict],
    source_record_ids: tuple[str, ...],
) -> tuple[Contract, Asset]:
    """Build an ordinary bounded task from a rejected output observation."""
    context_name = f"_fail_context_{failed_contract.id}"
    repair_instructions = (
        "The failed task has allowed tools. If no successful tool observation "
        "is disclosed, request an allowed tool before producing its declared "
        "outputs. Never invent tool-derived evidence."
        if failed_contract.tool_scope
        else (
            "Return exactly /exec with only the failed contract's declared "
            "outputs. Do not invent undeclared asset names. Use the rejection "
            "reasons to correct the output format and names."
        )
    )
    context = {
        "trigger": "projection_rejected",
        "failed_contract_id": failed_contract.id,
        "failed_contract_name": failed_contract.name,
        "bad_worker_output": candidate_raw[:4000],
        "rejections": rejections,
        "required_outputs": list(failed_contract.outputs),
        "instructions": repair_instructions,
        "source_record_ids": list(source_record_ids),
    }
    description = json.dumps(
        {
            "recovery": "projection_repair",
            "failed_contract_id": failed_contract.id,
            "source_record_ids": list(source_record_ids),
            "instructions": (
                f"Read {context_name}. {repair_instructions} Required outputs: "
                f"{list(failed_contract.outputs)}."
            ),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    authority = (
        context_name,
        *(
            output
            for output in failed_contract.outputs
            if output in failed_contract.minting_authority
        ),
    )
    asset = system_asset(
        name=context_name,
        content=json.dumps(context, sort_keys=True, ensure_ascii=False),
        created_by=funder.id,
        promptable=True,
    )
    policy = (
        dict(failed_contract.sensitive_input_policy)
        if failed_contract.sensitive_input_policy is not None
        else None
    )
    recovery = contract_from_fields(
        name=f"{failed_contract.name or failed_contract.id}.recover",
        description=description,
        inputs=_dedupe([context_name, *failed_contract.inputs]),
        outputs=list(failed_contract.outputs),
        activation=_append_activation(failed_contract.activation, context_name),
        budget=1,
        tool_scope=list(failed_contract.tool_scope),
        labels=list(failed_contract.labels),
        worker_capabilities=list(failed_contract.worker_capabilities),
        worker_pools=list(failed_contract.worker_pools),
        delegation_capabilities=list(failed_contract.delegation_capabilities),
        delegation_pools=list(failed_contract.delegation_pools),
        origin="recovery",
        parent_id=funder.id,
        minting_authority=authority,
        sensitive_input_policy=policy,
        acceptance_policy=(
            dict(failed_contract.acceptance_policy)
            if failed_contract.acceptance_policy is not None
            else None
        ),
        context_asset_ids=failed_contract.context_asset_ids,
    )
    return recovery, asset


def _generic_recovery_funder(store, failed_contract: Contract) -> Contract | None:
    from aigineering.core.causal_allowance import allowance_available

    contracts = {item.id: item for item in store.get_all_contracts()}
    records = tuple(record for _, record in store.scan_runtime_records())
    terminal_ids = {
        str(record.payload.get("contract_id"))
        for record in records
        if record.record_type == "lifecycle.terminal"
    }
    current = contracts.get(failed_contract.parent_id)
    visited: set[str] = set()
    while current is not None and current.id not in visited:
        visited.add(current.id)
        if current.id not in terminal_ids and _recovery_scope_fits(
            current, failed_contract
        ):
            if allowance_available(current, records) >= 1:
                return current
        current = contracts.get(current.parent_id)
    return None


def _method_payload(contract: Contract) -> dict:
    try:
        value = json.loads(contract.description)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _publish_recovery(
    store: StoreProtocol,
    publisher: CandidatePublisher,
    *,
    recovery: Contract,
    context_template: Asset,
    idempotency_key: str,
    causal_parents: tuple[str, ...],
    source_record_ids: tuple[str, ...] = (),
    recovery_snapshot=None,
) -> tuple[Contract, Asset] | None:
    effects = _recovery_effects(recovery, context_template)
    proof = _prove_existing_recovery(
        store,
        publisher,
        recovery=recovery,
        context_template=context_template,
        effects=effects,
        idempotency_key=idempotency_key,
        causal_parents=causal_parents,
        source_record_ids=source_record_ids,
        recovery_snapshot=recovery_snapshot,
    )
    if proof is not None:
        if proof.status == "matched":
            assert proof.contract is not None and proof.context_asset is not None
            return proof.contract, proof.context_asset
        if proof.status == "conflict":
            return None
    decision = publisher.publish(
        effects,
        idempotency_key=idempotency_key,
        causal_parents=causal_parents,
    )
    if not decision.accepted:
        from aigineering.plugins.recovery_history import RecoveryHistoryCaptureUnstable

        try:
            recaptured = _capture_recovery_history(publisher.store, publisher)
        except RecoveryHistoryCaptureUnstable:
            return None
        proof = _prove_existing_recovery(
            publisher.store,
            publisher,
            recovery=recovery,
            context_template=context_template,
            effects=effects,
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
            source_record_ids=source_record_ids,
            recovery_snapshot=recaptured,
        )
        if proof is None or proof.status != "matched":
            return None
        if proof.contract is None or proof.context_asset is None:
            return None
        return proof.contract, proof.context_asset
    if len(decision.contracts) != 1 or len(decision.assets) != 1:
        return None
    return decision.contracts[0], decision.assets[0]


def _recovery_effects(recovery: Contract, context_template: Asset):
    declaration = contract_declaration_effect(recovery)
    asset = asset_proposal_effect(context_template)
    return (declaration, asset)


def _prove_existing_recovery(
    store: StoreProtocol,
    publisher: CandidatePublisher,
    *,
    recovery: Contract,
    context_template: Asset,
    effects: tuple[CandidateEffect, ...],
    idempotency_key: str,
    causal_parents: tuple[str, ...],
    source_record_ids: tuple[str, ...] = (),
    recovery_snapshot=None,
) -> RecoveryPublicationProof | None:
    from aigineering.plugins.recovery_history import (
        RecoveryHistoryCaptureUnstable,
        RecoveryPublicationProof,
        prove_recovery_publication,
    )

    try:
        snapshot = recovery_snapshot or _capture_recovery_history(
            publisher.store, publisher
        )
    except RecoveryHistoryCaptureUnstable:
        return RecoveryPublicationProof("conflict", reason="history_capture_unstable")
    if snapshot is None:
        return None
    return prove_recovery_publication(
        snapshot,
        recovery=recovery,
        context_template=context_template,
        effects=effects,
        idempotency_key=idempotency_key,
        causal_parents=causal_parents,
        source_record_ids=source_record_ids,
    )


def _capture_recovery_history(store, publisher):
    from aigineering.plugins.recovery_history import capture_recovery_history

    return capture_recovery_history(store, publisher)


def _normalize_source_record_ids(record_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(record_ids, tuple) or any(
        not isinstance(record_id, str) or not record_id for record_id in record_ids
    ):
        raise ValueError("recovery source record IDs must be non-empty text")
    if len(record_ids) != len(set(record_ids)):
        raise ValueError("recovery source record IDs must be unique")
    return record_ids


def _causal_parents(
    contract_id: str, source_record_ids: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((contract_id, *source_record_ids)))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _append_activation(existing: str, context_name: str) -> str:
    if "_fail_context_" in existing:
        return context_name
    if existing.strip():
        return f"({existing}) AND {context_name}"
    return context_name
