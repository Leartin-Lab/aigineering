"""Immutable, single-snapshot inputs for aggregate task-view queries."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.runtime_projection import RuntimeProjection


class TaskViewQueryMismatch(ValueError):
    """A query context was reused with a different Store snapshot."""


class TaskViewReadFacade:
    """Narrow read-only view backed only by one captured fact snapshot."""

    __slots__ = (
        "_assets",
        "_assets_by_name",
        "_assets_by_id",
        "_contracts",
        "_contracts_by_id",
        "_records",
        "_traces",
        "_registrations",
        "_sealed",
    )

    def __init__(self, *, assets, contracts, records, traces, registrations):
        self._assets = tuple(assets)
        assets_by_name = defaultdict(list)
        for asset in self._assets:
            assets_by_name[asset.name].append(asset)
        self._assets_by_name = {
            key: tuple(value) for key, value in assets_by_name.items()
        }
        self._assets_by_id = {asset.id: asset for asset in self._assets}
        self._contracts = tuple(contracts)
        self._contracts_by_id = {contract.id: contract for contract in self._contracts}
        self._records = tuple(records)
        trace_index = defaultdict(list)
        for entry in traces:
            trace_index[entry.contract_id].append(entry)
        self._traces = {key: tuple(value) for key, value in trace_index.items()}
        self._registrations = tuple(registrations)
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("task-view read snapshot is immutable")
        object.__setattr__(self, name, value)

    def scan_runtime_records(
        self, *, after_revision=0, through_revision=None, record_type=None
    ):
        return [
            (revision, record)
            for revision, record in self._records
            if revision > after_revision
            and (through_revision is None or revision <= through_revision)
            and (record_type is None or record.record_type == record_type)
        ]

    def get_all_contracts(self):
        return list(self._contracts)

    def get_contract(self, contract_id):
        return self._contracts_by_id.get(contract_id)

    def get_all_assets(self):
        return list(self._assets)

    def get_asset(self, asset_id):
        return self._assets_by_id.get(asset_id)

    def get_assets_by_name(self, name):
        return list(self._assets_by_name.get(name, ()))

    def get_worker_registrations(self):
        return list(self._registrations)

    def get_by_contract(self, contract_id):
        return list(self._traces.get(contract_id, ()))


@dataclass(frozen=True)
class TaskViewQueryContext:
    """Complete immutable inputs captured from one operational read snapshot."""

    source_store: object
    domain_id: str | None
    revision: int
    runtime_records: tuple
    contracts: tuple
    assets: tuple
    registrations: tuple
    effect_projection: EffectProjectionContext
    read_store: TaskViewReadFacade
    runtime_projection: RuntimeProjection

    def require_match(self, store, *, projection_context=None):
        if store is not self.source_store:
            raise TaskViewQueryMismatch("task-view query belongs to a different Store")
        if store.get_runtime_revision() != self.revision:
            raise TaskViewQueryMismatch("task-view query Store revision has changed")
        if (
            self.domain_id is not None
            and getattr(store, "domain_id", self.domain_id) != self.domain_id
        ):
            raise TaskViewQueryMismatch("task-view query belongs to a different domain")
        if (
            projection_context is not None
            and projection_context is not self.effect_projection
        ):
            raise TaskViewQueryMismatch(
                "task-view projection context does not match its query snapshot"
            )


def load_task_view_query_context(store) -> TaskViewQueryContext:
    """Capture each persisted fact family once from the caller's read snapshot."""
    revision = store.get_runtime_revision()
    runtime_records = tuple(store.scan_runtime_records())
    if any(item_revision > revision for item_revision, _ in runtime_records):
        raise TaskViewQueryMismatch("runtime scan exceeds the captured revision")
    from aigineering.core.domain import GENESIS_RECORD_TYPE
    from aigineering.protocol.candidate import genesis_manifest_from_dict

    genesis = [
        record
        for _record_revision, record in runtime_records
        if record.record_type == GENESIS_RECORD_TYPE
    ]
    if len(genesis) > 1:
        raise TaskViewQueryMismatch("snapshot contains more than one Genesis record")
    domain_id = (
        genesis_manifest_from_dict(genesis[0].payload["manifest"]).id
        if genesis
        else None
    )
    contracts = tuple(store.get_all_contracts())
    assets = tuple(store.get_all_assets())
    registrations = tuple(store.get_worker_registrations())
    traces = tuple(store.get_all())
    claim_ids = frozenset(store.get_claim_history_contract_ids()) | frozenset(
        str(record.payload.get("contract_id", ""))
        for _record_revision, record in runtime_records
        if record.record_type == "claim.granted" and record.payload.get("contract_id")
    )
    if store.get_runtime_revision() != revision:
        raise TaskViewQueryMismatch("source Store changed while capturing its snapshot")
    effect_projection = EffectProjectionContext(
        contracts=contracts,
        assets=assets,
        runtime_records=tuple(record for _, record in runtime_records),
        registrations=registrations,
        claim_history_contract_ids=claim_ids,
    )
    read_store = TaskViewReadFacade(
        assets=assets,
        contracts=contracts,
        records=runtime_records,
        traces=traces,
        registrations=registrations,
    )
    runtime_projection = RuntimeProjection(
        read_store,
        read_store,
        runtime_records=runtime_records,
        available_names=frozenset(asset.name for asset in assets),
    )
    return TaskViewQueryContext(
        source_store=store,
        domain_id=domain_id,
        revision=revision,
        runtime_records=runtime_records,
        contracts=contracts,
        assets=assets,
        registrations=registrations,
        effect_projection=effect_projection,
        read_store=read_store,
        runtime_projection=runtime_projection,
    )
