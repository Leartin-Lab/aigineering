"""Immutable, single-snapshot inputs for aggregate task-view queries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.runtime_projection import RuntimeProjection


class TaskViewQueryMismatch(ValueError):
    """A query context was reused with a different Store snapshot."""


@dataclass(frozen=True)
class TaskViewQueryContext:
    """Frozen query inputs and narrow reader from one operational snapshot."""

    _matches_store: Callable[[object], bool] = field(repr=False)
    domain_id: str | None
    revision: int
    runtime_records: tuple
    contracts: tuple
    assets: tuple
    registrations: tuple
    effect_projection: EffectProjectionContext
    _traces: Mapping = field(repr=False, compare=False)
    runtime_projection: RuntimeProjection = field(init=False, repr=False, compare=False)
    _assets_by_name: Mapping = field(init=False, repr=False, compare=False)
    _assets_by_id: Mapping = field(init=False, repr=False, compare=False)
    _contracts_by_id: Mapping = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        assets_by_name = defaultdict(list)
        for asset in self.assets:
            assets_by_name[asset.name].append(asset)
        for name, values in (
            (
                "_assets_by_name",
                {key: tuple(value) for key, value in assets_by_name.items()},
            ),
            ("_assets_by_id", {asset.id: asset for asset in self.assets}),
            (
                "_contracts_by_id",
                {contract.id: contract for contract in self.contracts},
            ),
        ):
            object.__setattr__(self, name, MappingProxyType(values))
        object.__setattr__(
            self,
            "runtime_projection",
            RuntimeProjection(
                self,
                self,
                runtime_records=self.runtime_records,
                available_names=frozenset(asset.name for asset in self.assets),
            ),
        )

    def scan_runtime_records(
        self, *, after_revision=0, through_revision=None, record_type=None
    ):
        return [
            (revision, record)
            for revision, record in self.runtime_records
            if revision > after_revision
            and (through_revision is None or revision <= through_revision)
            and (record_type is None or record.record_type == record_type)
        ]

    def get_all_contracts(self):
        return list(self.contracts)

    def get_contract(self, contract_id):
        return self._contracts_by_id.get(contract_id)

    def read_task_facts(self, contract_id, record_types):
        return tuple(
            record
            for _, record in self.runtime_records
            if record.record_type in record_types
            and (
                record.payload.get("contract_id") == contract_id
                or (
                    record.record_type in {"allowance.reserved", "allowance.returned"}
                    and record.payload.get("source_contract_id") == contract_id
                )
                or (
                    record.record_type == "allowance.returned"
                    and record.payload.get("from_contract_id") == contract_id
                )
                or (
                    record.record_type == "contract.declared"
                    and record.payload["contract"]["id"] == contract_id
                )
            )
        )

    def get_all_assets(self):
        return list(self.assets)

    def get_asset(self, asset_id):
        return self._assets_by_id.get(asset_id)

    def get_assets_by_name(self, name):
        return list(self._assets_by_name.get(name, ()))

    def get_worker_registrations(self):
        return list(self.registrations)

    def get_by_contract(self, contract_id):
        return list(self._traces.get(contract_id, ()))

    def require_match(self, store, *, projection_context=None):
        if not self._matches_store(store):
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
    trace_index = defaultdict(list)
    for entry in traces:
        trace_index[entry.contract_id].append(entry)
    return TaskViewQueryContext(
        _matches_store=lambda supplied: supplied is store,
        domain_id=domain_id,
        revision=revision,
        runtime_records=runtime_records,
        contracts=contracts,
        assets=assets,
        registrations=registrations,
        effect_projection=effect_projection,
        _traces=MappingProxyType(
            {key: tuple(value) for key, value in trace_index.items()}
        ),
    )
