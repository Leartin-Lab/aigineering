"""Materialize pure FactReducer consequences as trace and runtime records."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from aigineering.core.trace import create_entry
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.wire import asset_to_dict, trace_entry_to_dict


def asset_committed_record(asset, *, causal_parents=()) -> RuntimeRecord:
    return create_runtime_record(
        "asset.committed",
        {"asset": asset_to_dict(asset), "contract_id": asset.created_by},
        causal_parents=causal_parents,
    )


def trace_records(entries: Sequence[object]) -> tuple[RuntimeRecord, ...]:
    return tuple(
        create_runtime_record(
            "trace.recorded",
            {"trace": trace_entry_to_dict(entry)},
        )
        for entry in entries
    )


def materialize_fact_reduction(
    events: Sequence[object], assets: Sequence[object]
) -> tuple[list[object], tuple[RuntimeRecord, ...]]:
    from aigineering.core.fact_reducer import FactReducerEvent

    assets_by_name = {asset.name: asset for asset in assets}
    traces: list[object] = []
    lifecycle: list[RuntimeRecord] = []
    for event in events:
        if not isinstance(event, FactReducerEvent) or not event.contract_id:
            continue
        asset = assets_by_name.get(event.asset_name)
        parent_id = asset.id if asset is not None else None
        trace_event_type: str | None = None
        trace_kwargs: dict[str, object] = {"relation_target": event.asset_name}

        if event.type == "contract_complete":
            trace_event_type = "complete"
            trace_kwargs["budget_remaining"] = 0
            lifecycle.append(
                create_runtime_record(
                    "lifecycle.terminal",
                    {"contract_id": event.contract_id, "terminal": "complete"},
                    causal_parents=(
                        (asset_committed_record(asset).id,) if asset is not None else ()
                    ),
                )
            )
        elif event.type == "child_cancelled":
            trace_event_type = "cancelled"
            trace_kwargs["relation_type"] = "unreachable"
            trace_kwargs["rejected_fragments"] = [
                "[unreachable] parent_complete: "
                f"parent {event.details.get('parent_id', '?')} completed"
            ]
            lifecycle.append(
                create_runtime_record(
                    "lifecycle.terminal",
                    {"contract_id": event.contract_id, "terminal": "cancelled"},
                    causal_parents=(
                        (asset_committed_record(asset).id,) if asset is not None else ()
                    ),
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


def reduce_asset_facts(store, trace, assets: Sequence[object], *, reducer=None):
    """Derive and materialize one atomic Asset batch through the pure reducer."""
    if reducer is None:
        from aigineering.core.fact_reducer import FactReducer

        reducer = FactReducer(store, trace)
    events = reducer.on_assets_created(tuple(assets))
    return materialize_fact_reduction(events, assets)
