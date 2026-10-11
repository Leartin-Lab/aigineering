"""Verified read-only containment and task-publication lineage."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from datetime import datetime, timezone

from aigineering.core.store import require_operational_store
from aigineering.core.publication_history import (
    index_published_records,
    read_effect_types,
    validate_published_member,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import validate_runtime_record
from aigineering.protocol.wire import asset_to_dict, contract_to_dict
from aigineering.task_diagnostics import (
    TaskDiagnosticError,
    task_invocation_failure_binding,
)
from aigineering.task_publication import validate_retry_publication_relations
from aigineering.plugins.handoff import validate_task_handoff_relations
from aigineering.plugins.task_revision import validate_task_revision_relations
from aigineering.task_view_query import (
    TaskViewQueryMismatch,
    load_task_view_query_context,
)
from aigineering.task_views import project_task_status

_RECOVERY_ACTOR = "plugin:recovery.publish.v1"
_RECOVERY_EFFECTS = ("contract.declare", "asset.propose")
_SCHEDULE_TYPES = frozenset({"worker_failure.recovery_scheduled"})


class TaskLineageError(ValueError):
    """Safe task-lineage query error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def task_lineage(store, selected_contract_id: str) -> dict:
    """Return one exact-snapshot component over verified publication links.

    Recovery links are accepted only when the original recovery publisher's
    receipt, domain, source causal parent, committed head, declaration and
    source-created context Asset all agree with the materialized Contracts.
    Revision links prove only their accepted source-to-successor relation;
    historical review decisions remain owned by the revision publisher.
    Failure and scheduling records are returned as source diagnostics; they
    are not paired one-to-one with a publication.
    """
    store = require_operational_store(store)
    if not isinstance(selected_contract_id, str) or not selected_contract_id:
        raise TaskLineageError("invalid_task_id")
    query = load_task_view_query_context(store)
    try:
        query.require_match(store)
    except TaskViewQueryMismatch as exc:
        raise TaskLineageError("snapshot_changed") from exc
    contracts = tuple(query.contracts)
    by_contract = {contract.id: contract for contract in contracts}
    if len(by_contract) != len(contracts):
        raise TaskLineageError("lineage_integrity_error")
    selected = by_contract.get(selected_contract_id)
    if selected is None:
        raise TaskLineageError("task_not_found")
    records = tuple(record for _revision, record in query.runtime_records)
    record_rows: dict[str, list[tuple[int, object]]] = defaultdict(list)
    for revision, record in query.runtime_records:
        record_rows[record.id].append((revision, record))
    duplicate_ids = {
        record_id for record_id, rows in record_rows.items() if len(rows) != 1
    }
    record_revisions = {
        record_id: rows[0][0]
        for record_id, rows in record_rows.items()
        if record_id not in duplicate_ids
    }
    by_id = {
        record_id: rows[0][1]
        for record_id, rows in record_rows.items()
        if record_id not in duplicate_ids
    }
    schedules_by_contract: dict[str, list[tuple[int, object]]] = defaultdict(list)
    grants_by_contract: dict[str, list[object]] = defaultdict(list)
    for revision, record in query.runtime_records:
        payload = record.payload
        contract_id = (
            payload.get("contract_id") if isinstance(payload, Mapping) else None
        )
        if not isinstance(contract_id, str):
            continue
        if record.record_type in _SCHEDULE_TYPES:
            schedules_by_contract[contract_id].append((revision, record))
        elif record.record_type == "claim.granted":
            grants_by_contract[contract_id].append(record)

    edges: list[dict] = []
    observation_time = datetime.now(timezone.utc).isoformat()
    claim_proof_cache = {}
    issues: list[dict] = [
        {"code": "duplicate_runtime_record_id", "record_id": record_id}
        for record_id in sorted(duplicate_ids)
    ]
    declaration_rows: dict[str, list] = defaultdict(list)
    for declaration in records:
        if declaration.record_type != "contract.declared":
            continue
        payload = declaration.payload
        declared = payload.get("contract") if isinstance(payload, Mapping) else None
        target_id = declared.get("id") if isinstance(declared, Mapping) else None
        if isinstance(target_id, str) and target_id:
            declaration_rows[target_id].append(declaration)

    publication_cache: dict[str, tuple] = {}
    verified_schedule_cache: dict[str, tuple] = {}
    publication_index = index_published_records(records)

    def publication_for(contract):
        cached = publication_cache.get(contract.id)
        if cached is not None:
            return cached
        declarations = declaration_rows.get(contract.id, ())
        if len(declarations) != 1:
            raise ValueError("declaration missing or ambiguous")
        declaration = declarations[0]
        receipt, head = validate_published_member(
            records,
            declaration,
            domain_id=query.domain_id,
            record_index=publication_index,
        )
        if deep_thaw(declaration.payload.get("contract")) != deep_thaw(
            contract_to_dict(contract)
        ):
            raise ValueError("declaration differs from materialized Contract")
        cached = (declaration, receipt, head)
        publication_cache[contract.id] = cached
        return cached

    for contract in contracts:
        try:
            declaration, receipt, head = publication_for(contract)
        except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
            issues.append(
                {
                    "code": "contract_publication_unverified",
                    "contract_id": contract.id,
                    **(
                        {"parent_contract_id": contract.parent_id}
                        if contract.parent_id is not None
                        else {}
                    ),
                }
            )
            continue
        if contract.parent_id is None:
            continue
        if contract.parent_id not in by_contract:
            issues.append(
                {"code": "containment_parent_missing", "contract_id": contract.id}
            )
            continue
        parent = by_contract[contract.parent_id]
        try:
            publication_for(parent)
        except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
            issues.append(
                {
                    "code": "containment_parent_unverified",
                    "contract_id": contract.id,
                    "parent_contract_id": parent.id,
                }
            )
            continue
        edges.append(
            {
                "kind": "containment",
                "source_contract_id": contract.parent_id,
                "target_contract_id": contract.id,
                "evidence_record_ids": sorted((receipt.id, declaration.id, head.id)),
                "receipt_id": receipt.id,
                "declaration_id": declaration.id,
                "head_id": head.id,
            }
        )

    for declaration in records:
        if declaration.record_type != "contract.declared":
            continue
        payload = declaration.payload
        declared = payload.get("contract") if isinstance(payload, Mapping) else None
        if not isinstance(declared, Mapping):
            continue
        target_id = declared.get("id")
        target = by_contract.get(target_id) if isinstance(target_id, str) else None
        if target is None or target.origin != "recovery":
            continue
        receipt_ids = tuple(declaration.causal_parents)
        declared_receipt = by_id.get(receipt_ids[0]) if len(receipt_ids) == 1 else None
        source_id = None
        if declared_receipt is not None:
            source_id = next(
                (
                    parent_id
                    for parent_id in declared_receipt.causal_parents
                    if parent_id in by_contract
                ),
                None,
            )
        try:
            proven_declaration, receipt, head = publication_for(target)
            if proven_declaration.id != declaration.id:
                raise ValueError("recovery declaration is ambiguous")
            source_ids = tuple(receipt.causal_parents)
            if (
                receipt.payload.get("actor_id") != _RECOVERY_ACTOR
                or read_effect_types(receipt.payload) != _RECOVERY_EFFECTS
                or len(source_ids) != 1
                or source_ids[0] not in by_contract
            ):
                raise ValueError("recovery publisher or source witness mismatch")
            source_id = source_ids[0]
            if deep_thaw(declared) != deep_thaw(contract_to_dict(target)):
                raise ValueError("recovery declaration differs from materialized task")
            source = by_contract[source_id]
            publication_for(source)
            member_ids = tuple(head.payload.get("committed_record_ids", ()))
            members = tuple(by_id[item] for item in member_ids)
            declarations = tuple(
                item for item in members if item.record_type == "contract.declared"
            )
            context_name = f"_fail_context_{source_id}"
            context_rows = tuple(
                item
                for item in members
                if item.record_type == "asset.committed"
                and isinstance(item.payload.get("asset"), Mapping)
                and item.payload["asset"].get("name") == context_name
                and item.payload["asset"].get("created_by") == source_id
            )
            context_assets = [
                asset
                for asset in query.assets
                if context_rows
                and asset.id == context_rows[0].payload["asset"].get("id")
            ]
            if len(context_rows) == 1 and (
                len(context_assets) != 1
                or deep_thaw(context_rows[0].payload["asset"])
                != deep_thaw(asset_to_dict(context_assets[0]))
            ):
                raise ValueError(
                    "recovery context Asset does not match materialization"
                )
            if (
                len(declarations) != 1
                or declarations[0].id != declaration.id
                or len(context_rows) != 1
                or any(
                    item.record_type
                    not in {
                        "contract.declared",
                        "asset.committed",
                        "asset.legacy-graph.migrated",
                        "allowance.granted",
                        "allowance.reserved",
                    }
                    for item in members
                )
            ):
                raise ValueError("recovery publication head has unexpected members")
            receipt_rows = record_rows.get(receipt.id, ())
            if len(receipt_rows) != 1:
                raise ValueError("recovery receipt revision is ambiguous")
            schedule_key = (source_id, receipt_rows[0][0])
            schedules = verified_schedule_cache.get(schedule_key)
            if schedules is None:
                schedules = _verified_source_schedules(
                    source_id,
                    schedules=schedules_by_contract.get(source_id, ()),
                    grants=grants_by_contract.get(source_id, ()),
                    query=query,
                    by_id=by_id,
                    proof_cache=claim_proof_cache,
                    observed_at=observation_time,
                    receipt_revision=receipt_rows[0][0],
                    record_revisions=record_revisions,
                )
                verified_schedule_cache[schedule_key] = schedules
            if not schedules:
                raise ValueError("no verified worker-failure recovery schedule")
            edges.append(
                {
                    "kind": "recovery_publication",
                    "source_contract_id": source_id,
                    "target_contract_id": target.id,
                    "evidence_record_ids": sorted(
                        (receipt.id, declaration.id, head.id, context_rows[0].id)
                    ),
                    "receipt_id": receipt.id,
                    "head_id": head.id,
                    "declaration_id": declaration.id,
                    "publisher_actor_id": _RECOVERY_ACTOR,
                    "diagnostic_schedule_record_ids": sorted(
                        record.id for _revision, record in schedules
                    ),
                }
            )
        except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
            # Invalid or ambiguous history stays visible in ordinary task views,
            # but never expands this verified component.
            issues.append(
                {
                    "code": "recovery_publication_unverified",
                    "contract_id": target.id,
                    **({"source_contract_id": source_id} if source_id else {}),
                }
            )

    retry_witnesses, retry_issues = validate_retry_publication_relations(
        query, records, publication_index
    )
    for witness in retry_witnesses:
        edges.append(
            {
                "kind": "retry_publication",
                "source_contract_id": witness.source_contract_id,
                "target_contract_id": witness.target_contract_id,
                "evidence_record_ids": sorted(
                    {
                        witness.source_declaration_id,
                        witness.source_receipt_id,
                        witness.source_head_id,
                        witness.target_declaration_id,
                        witness.target_receipt_id,
                        witness.target_head_id,
                    }
                ),
                "source_declaration_id": witness.source_declaration_id,
                "source_receipt_id": witness.source_receipt_id,
                "source_head_id": witness.source_head_id,
                "target_declaration_id": witness.target_declaration_id,
                "target_receipt_id": witness.target_receipt_id,
                "target_head_id": witness.target_head_id,
                "candidate_id": witness.candidate_id,
            }
        )
    issues.extend(
        {
            "code": issue.code,
            **(
                {"target_contract_id": issue.target_contract_id}
                if issue.target_contract_id is not None
                else {}
            ),
            **(
                {"source_contract_id": issue.source_contract_id}
                if issue.source_contract_id is not None
                else {}
            ),
            **({"record_id": issue.record_id} if issue.record_id else {}),
        }
        for issue in retry_issues
    )

    handoff_witnesses, handoff_issues = validate_task_handoff_relations(
        query, records, publication_index
    )
    for witness in handoff_witnesses:
        edges.append(
            {
                "kind": "handoff_publication",
                "source_contract_id": witness.source_contract_id,
                "target_contract_id": witness.target_contract_id,
                "evidence_record_ids": sorted(
                    {
                        witness.source_declaration_id,
                        witness.source_receipt_id,
                        witness.source_head_id,
                        witness.handoff_link_id,
                        witness.handoff_receipt_id,
                        witness.handoff_head_id,
                        witness.cancelled_terminal_id,
                        witness.target_declaration_id,
                        witness.target_receipt_id,
                        witness.target_head_id,
                    }
                ),
                "source_declaration_id": witness.source_declaration_id,
                "source_receipt_id": witness.source_receipt_id,
                "source_head_id": witness.source_head_id,
                "handoff_link_id": witness.handoff_link_id,
                "handoff_receipt_id": witness.handoff_receipt_id,
                "handoff_head_id": witness.handoff_head_id,
                "cancelled_terminal_id": witness.cancelled_terminal_id,
                "target_declaration_id": witness.target_declaration_id,
                "target_receipt_id": witness.target_receipt_id,
                "target_head_id": witness.target_head_id,
            }
        )
    issues.extend(
        {
            "code": issue.code,
            **(
                {"source_contract_id": issue.source_contract_id}
                if issue.source_contract_id is not None
                else {}
            ),
            **(
                {"target_contract_id": issue.target_contract_id}
                if issue.target_contract_id is not None
                else {}
            ),
            **({"record_id": issue.record_id} if issue.record_id else {}),
        }
        for issue in handoff_issues
    )

    revision_witnesses, revision_issues = validate_task_revision_relations(
        query, records, publication_index
    )
    for witness in revision_witnesses:
        edges.append(
            {
                "kind": "revision_publication",
                "source_contract_id": witness.source_contract_id,
                "target_contract_id": witness.target_contract_id,
                "evidence_record_ids": sorted(
                    {
                        witness.source_declaration_id,
                        witness.source_receipt_id,
                        witness.source_head_id,
                        witness.revision_link_id,
                        witness.revision_receipt_id,
                        witness.revision_head_id,
                        witness.cancelled_terminal_id,
                        witness.target_declaration_id,
                        witness.target_receipt_id,
                        witness.target_head_id,
                    }
                ),
                "source_declaration_id": witness.source_declaration_id,
                "source_receipt_id": witness.source_receipt_id,
                "source_head_id": witness.source_head_id,
                "revision_link_id": witness.revision_link_id,
                "revision_receipt_id": witness.revision_receipt_id,
                "revision_head_id": witness.revision_head_id,
                "cancelled_terminal_id": witness.cancelled_terminal_id,
                "target_declaration_id": witness.target_declaration_id,
                "target_receipt_id": witness.target_receipt_id,
                "target_head_id": witness.target_head_id,
            }
        )
    issues.extend(
        {
            "code": issue.code,
            **(
                {"source_contract_id": issue.source_contract_id}
                if issue.source_contract_id is not None
                else {}
            ),
            **(
                {"target_contract_id": issue.target_contract_id}
                if issue.target_contract_id is not None
                else {}
            ),
            **({"record_id": issue.record_id} if issue.record_id else {}),
        }
        for issue in revision_issues
    )

    # A repeated source/target edge is not resolved by first-match behavior.
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    pair_kinds: dict[tuple[str, str], set[str]] = defaultdict(set)
    for edge in edges:
        if edge["kind"] in {
            "recovery_publication",
            "retry_publication",
            "handoff_publication",
            "revision_publication",
        }:
            pair = (edge["source_contract_id"], edge["target_contract_id"])
            pair_counts[pair] += 1
            pair_kinds[pair].add(edge["kind"])
    duplicate_pairs = {pair for pair, count in pair_counts.items() if count != 1}
    if duplicate_pairs:
        edges = [
            edge
            for edge in edges
            if edge["kind"]
            not in {
                "recovery_publication",
                "retry_publication",
                "handoff_publication",
                "revision_publication",
            }
            or (edge["source_contract_id"], edge["target_contract_id"])
            not in duplicate_pairs
        ]
        for source_id, target_id in sorted(duplicate_pairs):
            if "handoff_publication" in pair_kinds[(source_id, target_id)]:
                issues.append(
                    {
                        "code": "handoff_publication_ambiguous",
                        "source_contract_id": source_id,
                        "target_contract_id": target_id,
                    }
                )
            if "revision_publication" in pair_kinds[(source_id, target_id)]:
                issues.append(
                    {
                        "code": "revision_publication_ambiguous",
                        "source_contract_id": source_id,
                        "target_contract_id": target_id,
                    }
                )
            if not pair_kinds[(source_id, target_id)].intersection(
                {"handoff_publication", "revision_publication"}
            ):
                issues.append(
                    {
                        "code": "recovery_publication_ambiguous",
                        "contract_id": target_id,
                    }
                )

    adjacent: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        source_id, target_id = edge["source_contract_id"], edge["target_contract_id"]
        adjacent[source_id].add(target_id)
        adjacent[target_id].add(source_id)
    component: set[str] = set()
    queue = deque([selected_contract_id])
    while queue:
        current = queue.popleft()
        if current in component:
            continue
        component.add(current)
        queue.extend(sorted(adjacent.get(current, ())))

    component_edges = [
        edge
        for edge in edges
        if edge["source_contract_id"] in component
        and edge["target_contract_id"] in component
    ]
    issues.extend(
        {"code": "lineage_cycle", "contract_id": contract_id}
        for contract_id in _cycle_members(component_edges)
    )
    component_ids = sorted(component)
    cards = []
    for contract_id in component_ids:
        contract = by_contract[contract_id]
        try:
            status = project_task_status(contract, store, query_context=query)
        except TaskViewQueryMismatch as exc:
            raise TaskLineageError("snapshot_changed") from exc
        cards.append(
            {
                "contract_id": contract_id,
                "parent_id": contract.parent_id,
                "name": contract.name,
                "origin": contract.origin,
                "task": status,
            }
        )
    diagnostics, diagnostic_issues = _source_diagnostics(
        component_ids,
        query,
        component_edges,
        proof_cache=claim_proof_cache,
        observed_at=observation_time,
    )
    issues.extend(diagnostic_issues)
    issues = [issue for issue in issues if _issue_touches_component(issue, component)]
    if store.get_runtime_revision() != query.revision:
        raise TaskLineageError("snapshot_changed")
    return {
        "ok": True,
        "data": {
            "selected_contract_id": selected_contract_id,
            "domain_id": query.domain_id,
            "revision": query.revision,
            "covered_relations": [
                "containment",
                "recovery_publication",
                "retry_publication",
                "handoff_publication",
                "revision_publication",
            ],
            "contract_ids": component_ids,
            "contracts": cards,
            "edges": sorted(
                component_edges,
                key=lambda edge: (
                    edge["kind"],
                    edge["source_contract_id"],
                    edge["target_contract_id"],
                ),
            ),
            "source_diagnostics": diagnostics,
            "issues": sorted(
                issues,
                key=lambda item: (
                    item.get("contract_id", ""),
                    item.get("record_id", ""),
                    item["code"],
                ),
            ),
        },
        "revision": query.revision,
    }


def _issue_touches_component(issue, component):
    references = tuple(
        issue.get(field)
        for field in (
            "contract_id",
            "source_contract_id",
            "target_contract_id",
            "parent_contract_id",
        )
        if isinstance(issue.get(field), str)
    )
    return not references or any(reference in component for reference in references)


def _source_diagnostics(contract_ids, query, edges, *, proof_cache, observed_at):
    result = []
    issues = []
    recovery_targets = {
        edge["target_contract_id"]
        for edge in edges
        if edge["kind"] == "recovery_publication"
    }
    records_by_contract: dict[str, list] = defaultdict(list)
    for revision, record in query.runtime_records:
        payload = record.payload
        contract_id = (
            payload.get("contract_id") if isinstance(payload, Mapping) else None
        )
        if isinstance(contract_id, str) and contract_id in contract_ids:
            records_by_contract[contract_id].append((revision, record))
    for contract_id in contract_ids:
        schedule_rows = [
            (revision, record)
            for revision, record in records_by_contract[contract_id]
            if record.record_type in _SCHEDULE_TYPES
        ]
        grants = [
            record
            for _revision, record in records_by_contract[contract_id]
            if record.record_type == "claim.granted"
        ]
        by_id = {record.id: record for _revision, record in query.runtime_records}
        record_revisions = {
            record.id: revision for revision, record in query.runtime_records
        }
        try:
            verified_schedules = _verified_source_schedules(
                contract_id,
                schedules=schedule_rows,
                grants=grants,
                query=query,
                by_id=by_id,
                proof_cache=proof_cache,
                observed_at=observed_at,
                receipt_revision=None,
                record_revisions=record_revisions,
            )
        except TaskLineageError:
            raise
        except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
            verified_schedules = ()
        verified_ids = {record.id for _revision, record in verified_schedules}
        schedules = [
            {
                "record_id": record.id,
                "revision": revision,
                "record_type": record.record_type,
            }
            for revision, record in verified_schedules
        ]
        for revision, record in schedule_rows:
            if record.id not in verified_ids:
                issues.append(
                    {
                        "code": "recovery_schedule_unverified",
                        "contract_id": contract_id,
                        "record_id": record.id,
                    }
                )
        unverified_rows = [
            (revision, record)
            for revision, record in schedule_rows
            if record.id not in verified_ids
        ]
        if unverified_rows:
            result.append(
                {
                    "contract_id": contract_id,
                    "unverified_schedule_observations": [
                        {
                            "record_id": record.id,
                            "revision": revision,
                            "record_type": record.record_type,
                            "verification": "unverified_observation",
                        }
                        for revision, record in unverified_rows
                    ],
                }
            )
        if schedules:
            result.append({"contract_id": contract_id, "schedule_records": schedules})
        trace_observations = [
            {
                "trace_id": entry.id,
                "event_type": entry.event_type,
                "relation_type": entry.relation_type,
                "relation_target": entry.relation_target,
            }
            for entry in query.read_store.get_by_contract(contract_id)
            if entry.relation_target in recovery_targets
            and entry.event_type
            in {"recovery_scheduled", "method_recovery_scheduled", "failed"}
        ]
        if trace_observations:
            result.append(
                {
                    "contract_id": contract_id,
                    "uncorrelated_trace_observations": trace_observations,
                }
            )
        failures = []
        for grant in grants:
            payload = grant.payload
            try:
                binding = task_invocation_failure_binding(
                    query.source_store,
                    contract_id,
                    payload.get("claim_id"),
                    payload.get("package_id"),
                    payload.get("worker_id"),
                    payload.get("epoch"),
                    query_context=query,
                    _proof_cache=proof_cache,
                    _observed_at=observed_at,
                )
            except TaskViewQueryMismatch as exc:
                raise TaskLineageError("snapshot_changed") from exc
            except TaskDiagnosticError:
                continue
            failures.append(binding)
        if failures:
            result.append({"contract_id": contract_id, "invocation_failures": failures})
    return result, issues


def _verified_source_schedules(
    source_id,
    *,
    schedules,
    grants,
    query,
    by_id,
    proof_cache,
    observed_at,
    receipt_revision,
    record_revisions,
):
    grants_by_claim = {
        record.payload.get("claim_id"): record
        for record in grants
        if isinstance(record.payload, Mapping)
    }
    verified = []
    for revision, schedule in schedules:
        if (
            schedule.record_type != "worker_failure.recovery_scheduled"
            or not isinstance(schedule.payload, Mapping)
            or schedule.payload.get("contract_id") != source_id
        ):
            continue
        try:
            validate_runtime_record(schedule)
        except (TypeError, ValueError):
            continue
        field, expected_type = {
            "worker_failure.recovery_scheduled": (
                "failure_id",
                "worker.invocation_failed",
            ),
        }[schedule.record_type]
        source_record_id = schedule.payload.get(field)
        source_record = by_id.get(source_record_id)
        if (
            not isinstance(source_record_id, str)
            or source_record is None
            or source_record.record_type != expected_type
            or not isinstance(source_record.payload, Mapping)
            or source_record.payload.get("contract_id") != source_id
            or tuple(schedule.causal_parents) != (source_record_id,)
        ):
            continue
        if schedule.record_type == "worker_failure.recovery_scheduled":
            claim_id = source_record.payload.get("claim_id")
            grant = grants_by_claim.get(claim_id)
            if grant is None:
                continue
            try:
                binding = task_invocation_failure_binding(
                    query.source_store,
                    source_id,
                    grant.payload.get("claim_id"),
                    grant.payload.get("package_id"),
                    grant.payload.get("worker_id"),
                    grant.payload.get("epoch"),
                    query_context=query,
                    _proof_cache=proof_cache,
                    _observed_at=observed_at,
                )
            except TaskViewQueryMismatch as exc:
                raise TaskLineageError("snapshot_changed") from exc
            except TaskDiagnosticError:
                continue
            if binding.get("failure_record_id") != source_record_id:
                continue
            before_receipt = (
                binding.get("grant_record_id"),
                binding.get("failure_record_id"),
                binding.get("release_record_id"),
            )
            revisions = tuple(record_revisions.get(item) for item in before_receipt)
            if receipt_revision is not None and any(
                item is None or item >= receipt_revision for item in revisions
            ):
                continue
        verified.append((revision, schedule))
    return verified


def _cycle_members(edges) -> set[str]:
    adjacent: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        adjacent[edge["source_contract_id"]].append(edge["target_contract_id"])
    state: dict[str, int] = {}
    cyclic: set[str] = set()
    for contract_id in sorted(adjacent):
        if state.get(contract_id) == 2:
            continue
        path = [contract_id]
        positions = {contract_id: 0}
        state[contract_id] = 1
        frames = [(contract_id, iter(sorted(adjacent.get(contract_id, ()))))]
        while frames:
            current, children = frames[-1]
            child_id = next(children, None)
            if child_id is None:
                frames.pop()
                path.pop()
                positions.pop(current, None)
                state[current] = 2
                continue
            if state.get(child_id) == 1:
                cyclic.update(path[positions[child_id] :])
            elif state.get(child_id) != 2:
                positions[child_id] = len(path)
                path.append(child_id)
                state[child_id] = 1
                frames.append((child_id, iter(sorted(adjacent.get(child_id, ())))))
    return cyclic
