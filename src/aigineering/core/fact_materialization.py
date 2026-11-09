"""Materialize pure FactReducer consequences as trace and runtime records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from aigineering.core.candidate_decision import trace_record
from aigineering.core.trace import create_entry
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.wire import asset_to_dict


def asset_committed_record(asset, *, causal_parents=()) -> RuntimeRecord:
    return create_runtime_record(
        "asset.committed",
        {"asset": asset_to_dict(asset), "contract_id": asset.created_by},
        causal_parents=causal_parents,
    )


def trace_records(entries: Sequence[object]) -> tuple[RuntimeRecord, ...]:
    return tuple(trace_record(entry) for entry in entries)


def materialize_fact_reduction(
    events: Sequence[object],
    assets: Sequence[object],
    *,
    completion_witnesses: Mapping[str, tuple[str, ...]],
) -> tuple[list[object], tuple[RuntimeRecord, ...]]:
    from aigineering.core.fact_reducer import FactReducerEvent

    assets_by_name = {asset.name: asset for asset in assets}
    traces: list[object] = []
    completed = {
        event.contract_id: create_terminal_record(
            event.contract_id,
            "complete",
            causal_parents=completion_witnesses[event.contract_id],
        )
        for event in events
        if event.type == "contract_complete"
        and not event.details.get("historical_terminal")
    }
    lifecycle: list[RuntimeRecord] = []
    for event in events:
        if not isinstance(event, FactReducerEvent) or not event.contract_id:
            continue
        if event.type in {"contract_complete", "child_cancelled"} and event.details.get(
            "historical_terminal"
        ):
            continue
        asset = assets_by_name.get(event.asset_name)
        parent_id = asset.id if asset is not None else None
        trace_event_type: str | None = None
        trace_kwargs: dict[str, object] = {"relation_target": event.asset_name}

        if event.type == "contract_complete":
            trace_event_type = "complete"
            trace_kwargs["budget_remaining"] = 0
            if event.contract_id in completed:
                lifecycle.append(completed[event.contract_id])
        elif event.type == "child_cancelled":
            trace_event_type = "cancelled"
            trace_kwargs["relation_type"] = "unreachable"
            trace_kwargs["rejected_fragments"] = [
                "[unreachable] parent_complete: "
                f"parent {event.details.get('parent_id', '?')} completed"
            ]
            if not event.details.get("historical_terminal"):
                completion = completed.get(event.asset_name)
                causes = (
                    (completion.id,)
                    if completion is not None
                    else completion_witnesses[event.asset_name]
                )
                lifecycle.append(
                    create_terminal_record(
                        event.contract_id,
                        "cancelled",
                        causal_parents=causes,
                    )
                )
        elif event.type == "output_satisfied":
            trace_event_type = "output_satisfied"
        elif event.type == "activation_active":
            trace_event_type = "activation"
        elif event.type == "method_result_detected":
            trace_event_type = "method_result_detected"

        if trace_event_type is not None:
            traces.append(
                replace(
                    create_entry(
                        contract_id=event.contract_id,
                        event_type=trace_event_type,
                        parent_id=parent_id,
                        **trace_kwargs,
                    ),
                    timestamp="",
                )
            )
    return traces, tuple(lifecycle)


def reduce_admitted_facts(store, assets, contracts, pending_records):
    """Use the sole reducer for Assets, new Tasks and policy qualification."""
    from aigineering.core.fact_reducer import FactReducer
    from aigineering.core.output_satisfaction import selected_output_assets

    if (
        not assets
        and not contracts
        and not any(row.record_type == "output.qualified" for row in pending_records)
    ):
        return (), ()
    pending_ids: dict[tuple[str, str], set[str]] = {}
    triggers = {asset.id: asset for asset in assets}
    for record in pending_records:
        if record.record_type == "output.qualified":
            slot = (record.payload["contract_id"], record.payload["output_name"])
            asset_id = record.payload["asset_id"]
            pending_ids.setdefault(slot, set()).add(asset_id)
            asset = store.get_asset(asset_id)
            if asset is None:
                raise ValueError("qualified Asset is absent from the transaction")
            triggers[asset_id] = asset
    events = FactReducer(store).on_assets_created(
        tuple(triggers.values()),
        pending_contracts=tuple(contracts),
        pending_runtime_records=tuple(pending_records),
        extra_qualified_asset_ids=pending_ids,
    )
    witnesses: dict[str, tuple[str, ...]] = {}
    for event in events:
        if event.type != "contract_complete":
            continue
        contract = store.get_contract(event.contract_id)
        selected = selected_output_assets(
            contract, store, extra_qualified_asset_ids=pending_ids
        )
        facts = (
            *store.read_task_facts(
                contract.id, ("contract.declared", "output.qualified")
            ),
            *(
                row
                for row in pending_records
                if (
                    row.record_type == "contract.declared"
                    and row.payload["contract"]["id"] == contract.id
                )
                or (
                    row.record_type == "output.qualified"
                    and row.payload["contract_id"] == contract.id
                )
            ),
        )
        causes = [row.id for row in facts if row.record_type == "contract.declared"]
        if not causes or len(selected) != len(set(contract.outputs)):
            raise ValueError(
                "completion lacks its actual declaration and Asset witnesses"
            )
        for output, asset in selected.items():
            committed = tuple(
                row.id
                for row in (
                    *store.read_asset_commit_facts(asset.id),
                    *(
                        row
                        for row in pending_records
                        if row.record_type == "asset.committed"
                        and row.payload["asset"]["id"] == asset.id
                    ),
                )
            )
            if not committed:
                raise ValueError("completion Asset lacks an actual committed witness")
            causes.extend(committed)
            if (contract.acceptance_policy or {}).get("mode") == "independent":
                qualified = tuple(
                    row.id
                    for row in facts
                    if row.record_type == "output.qualified"
                    and row.payload["output_name"] == output
                    and row.payload["asset_id"] == asset.id
                )
                if not qualified:
                    raise ValueError("completion lacks its exact qualification witness")
                causes.extend(qualified)
        witnesses[contract.id] = tuple(dict.fromkeys(causes))
    return materialize_fact_reduction(
        events, tuple(triggers.values()), completion_witnesses=witnesses
    )
