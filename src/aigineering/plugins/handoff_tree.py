"""Deterministic inventory for expanded task handoff projections."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from aigineering.core.claim_holds import open_claim_holds
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.task_lifecycle import serves_pending_independent_acceptance
from aigineering.plugins.task_successors import (
    ProjectionStoreView,
    satisfied_output_slots,
)
from aigineering.protocol.types import Contract
from aigineering.protocol.wire import trace_entry_from_dict


_HISTORICAL_FAILURES = frozenset({"failed", "cancelled", "unreachable"})


@dataclass(frozen=True)
class HandoffTreeInventory:
    """Immutable classifications from one handoff projection snapshot."""

    cancelled: tuple[Contract, ...]
    repairs: tuple[Contract, ...]
    preserved: tuple[str, ...]
    blockers: tuple[str, ...]
    repair_outputs: tuple[
        tuple[str, tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]], ...
    ]

    @property
    def expanded(self) -> bool:
        return bool(self.cancelled or self.repairs or self.preserved)


def inventory_handoff_tree(
    source: Contract,
    context: EffectProjectionContext,
    *,
    allow_source_holds: bool = False,
) -> HandoffTreeInventory:
    """Classify live descendants without changing their durable facts.

    Completed-but-untyped history is represented as a ``complete`` repair.
    A legacy adverse terminal trace without its typed fact is a blocker; it is
    never silently reinterpreted as either active work or a completion.
    """
    contracts = {item.id: item for item in context.contracts}
    contracts.setdefault(source.id, source)
    children: dict[str, list[Contract]] = {}
    for item in contracts.values():
        if item.parent_id:
            children.setdefault(item.parent_id, []).append(item)
    for values in children.values():
        values.sort(key=lambda item: item.id)

    typed_terminal = {
        str(record.payload.get("contract_id", "")): str(
            record.payload.get("terminal", "")
        )
        for record in context.runtime_records
        if record.record_type == "lifecycle.terminal"
    }
    adverse_trace_ids: dict[str, list[str]] = {}
    for record in context.runtime_records:
        if record.record_type != "trace.recorded":
            continue
        raw = record.payload.get("trace")
        if not isinstance(raw, Mapping):
            continue
        try:
            entry = trace_entry_from_dict(dict(raw))
        except (TypeError, ValueError, KeyError):
            continue
        if (
            entry.event_type in _HISTORICAL_FAILURES
            and entry.contract_id not in typed_terminal
        ):
            adverse_trace_ids.setdefault(entry.contract_id, []).append(record.id)

    holds = open_claim_holds(context.runtime_records)
    holds_by_contract: dict[str, list[str]] = {}
    for hold in holds:
        holds_by_contract.setdefault(str(hold.payload["contract_id"]), []).append(
            hold.id
        )

    contracts_by_id = dict(contracts)
    closed = set(typed_terminal)
    # The source is cancelled by this operation and cannot keep an internal
    # verifier branch alive. External live acceptance ancestors can.
    closed.add(source.id)
    cancelled: list[Contract] = []
    repairs: list[Contract] = []
    preserved: list[str] = []
    repair_outputs: list[
        tuple[str, tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]]
    ] = []
    blockers: list[str] = []
    queue: list[tuple[Contract, Contract]] = [
        (source, child) for child in children.get(source.id, ())
    ]
    visited: set[str] = set()
    view = ProjectionStoreView(context)

    while queue:
        parent, child = queue.pop(0)
        if child.id in visited:
            continue
        visited.add(child.id)

        if child.id not in typed_terminal and serves_pending_independent_acceptance(
            child, parent, contracts_by_id, closed
        ):
            preserved.extend(_subtree_ids(child, children))
            continue

        # A historical failed/cancelled trace with no typed lifecycle fact is
        # ambiguous and must be resolved by explicit historical repair first.
        # This check follows preservation because external verifier branches
        # are outside the selected transfer, including their holds/history.
        if child.id not in typed_terminal and child.id in adverse_trace_ids:
            blockers.append(
                f"history conflict Contract {child.id}: trace records "
                + ", ".join(sorted(adverse_trace_ids[child.id]))
            )
            if child.id in holds_by_contract:
                blockers.append(
                    f"open hold on Contract {child.id}: "
                    + ", ".join(sorted(holds_by_contract[child.id]))
                )
            continue

        if child.id in typed_terminal:
            closed.add(child.id)
        else:
            satisfied = all_outputs_satisfied(child, view, require_outputs=True)
            if satisfied:
                repairs.append(child)
                closed.add(child.id)
                slots = []
                for output_name, asset_ids in satisfied_output_slots(child, context):
                    proof_ids = {
                        record.id
                        for record in context.runtime_records
                        if record.record_type == "asset.committed"
                        and isinstance(record.payload.get("asset"), Mapping)
                        and record.payload["asset"].get("id") in asset_ids
                    }
                    proof_ids.update(
                        record.id
                        for record in context.runtime_records
                        if record.record_type == "output.qualified"
                        and record.payload.get("contract_id") == child.id
                        and record.payload.get("output_name") == output_name
                        and record.payload.get("asset_id") in asset_ids
                    )
                    slots.append((output_name, asset_ids, tuple(sorted(proof_ids))))
                repair_outputs.append((child.id, tuple(slots)))
            else:
                cancelled.append(child)
                closed.add(child.id)

        if child.id not in typed_terminal and child.id in holds_by_contract:
            blockers.append(
                f"open hold on Contract {child.id}: "
                + ", ".join(sorted(holds_by_contract[child.id]))
            )
        queue.extend((child, descendant) for descendant in children.get(child.id, ()))

    # Normal handoff cannot strand a source hold on the cancelled Contract.
    # Text-edit handoff may instead transfer the complete snapshot atomically.
    if source.id in holds_by_contract and not allow_source_holds:
        blockers.append(
            f"open hold on Contract {source.id}: "
            + ", ".join(sorted(holds_by_contract[source.id]))
        )
    if source.id in adverse_trace_ids and source.id not in typed_terminal:
        blockers.append(
            f"history conflict Contract {source.id}: trace records "
            + ", ".join(sorted(adverse_trace_ids[source.id]))
        )
    if blockers:
        raise ValueError("task.handoff blocked: " + "; ".join(sorted(blockers)))

    return HandoffTreeInventory(
        cancelled=tuple(cancelled),
        repairs=tuple(repairs),
        preserved=tuple(sorted(set(preserved))),
        blockers=(),
        repair_outputs=tuple(repair_outputs),
    )


def _subtree_ids(
    root: Contract, children: Mapping[str, list[Contract]]
) -> tuple[str, ...]:
    result: list[str] = []
    queue = [root]
    seen: set[str] = set()
    while queue:
        item = queue.pop(0)
        if item.id in seen:
            continue
        seen.add(item.id)
        result.append(item.id)
        queue.extend(children.get(item.id, ()))
    return tuple(result)
