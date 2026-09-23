"""Root-tree scoping over interleaved durable runtime records."""

import json

from aigineering.cli._common import _session_scope_for_run
from aigineering.core.trace import (
    MemoryTraceStore,
    create_entry,
    trace_record_from_entry,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import asset_to_dict


class _SnapshotStore:
    def __init__(self, contracts, assets, records):
        self._contracts = contracts
        self._assets = {asset.id: asset for asset in assets}
        self._records = records

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

    def get_asset(self, asset_id):
        return self._assets.get(asset_id)

    def get_assets_by_name(self, name):
        return [asset for asset in self._assets.values() if asset.name == name]


def _trace_record(
    contract_id, event_type, *, accepted_fragments=(), disclosed_assets=()
):
    return trace_record_from_entry(
        create_entry(
            contract_id,
            event_type,
            accepted_fragments=list(accepted_fragments),
            disclosed_assets=list(disclosed_assets),
        )
    )


def test_session_scope_includes_existing_descendant_and_excludes_interleaved_root():
    root = Contract(
        id="root",
        inputs=("root_input",),
        context_asset_ids=("root_input_asset",),
    )
    child = Contract(
        id="child",
        parent_id="root",
        inputs=("child_input",),
        context_asset_ids=("child_input_asset",),
    )
    legacy_child = Contract(
        id="legacy-child",
        parent_id="root",
        inputs=("duplicate",),
    )
    other_root = Contract(id="other-root")
    root_input = Asset(id="root_input_asset", name="root_input", content="history")
    child_input = Asset(id="child_input_asset", name="child_input", content="history")
    produced = Asset(id="produced", name="output", content="new", created_by="child")
    duplicate_seen = Asset(id="duplicate-seen", name="duplicate", content="used")
    duplicate_unseen = Asset(
        id="duplicate-unseen", name="duplicate", content="not-used"
    )

    root_declaration = create_runtime_record(
        "contract.declared", {"contract": {"id": "root", "parent_id": None}}
    )
    child_declaration = create_runtime_record(
        "contract.declared", {"contract": {"id": "child", "parent_id": "root"}}
    )
    other_declaration = create_runtime_record(
        "contract.declared",
        {"contract": {"id": "other-root", "parent_id": None}},
    )
    qualification = create_runtime_record(
        "output.qualified", {"contract_id": "root", "asset_id": "produced"}
    )
    other_qualification = create_runtime_record(
        "output.qualified", {"contract_id": "other-root", "asset_id": "elsewhere"}
    )
    committed = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": "candidate:root-result",
            "committed_record_ids": [qualification.id],
        },
    )
    other_committed = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": "candidate:other-result",
            "committed_record_ids": [other_qualification.id],
        },
    )
    sentinel_trace = _trace_record(
        "commitment",
        "candidate_committed",
        accepted_fragments=[json.dumps({"candidate_id": "candidate:root-result"})],
    )
    other_sentinel_trace = _trace_record(
        "commitment",
        "candidate_committed",
        accepted_fragments=[json.dumps({"candidate_id": "candidate:other-result"})],
    )
    root_trace = _trace_record("root", "activation")
    other_trace = _trace_record("other-root", "activation")
    child_trace = _trace_record("child", "projection")
    legacy_child_trace = _trace_record(
        "legacy-child",
        "disclosure",
        disclosed_assets=(duplicate_seen.id,),
    )
    unassigned_trace = _trace_record(
        "commitment",
        "candidate_committed",
        accepted_fragments=[json.dumps({"candidate_id": "candidate:unknown"})],
    )
    output_record = create_runtime_record(
        "asset.committed",
        {"contract_id": "child", "asset": asset_to_dict(produced)},
    )
    records = [
        (1, root_declaration),
        (2, child_declaration),
        (3, other_declaration),
        (4, qualification),
        (5, committed),
        (6, sentinel_trace),
        (7, other_qualification),
        (8, other_committed),
        (9, other_sentinel_trace),
        (10, root_trace),
        (11, other_trace),
        (12, child_trace),
        (13, legacy_child_trace),
        (14, output_record),
        (15, unassigned_trace),
    ]
    store = _SnapshotStore(
        [root, child, legacy_child, other_root],
        [
            root_input,
            child_input,
            produced,
            duplicate_seen,
            duplicate_unseen,
        ],
        records,
    )
    trace_store = MemoryTraceStore()

    scope = _session_scope_for_run(store, trace_store, "root", 3, 15)

    assert scope.contract_ids == ("child", "legacy-child", "root")
    assert set(scope.trace_ids) == {
        sentinel_trace.payload["trace"]["id"],
        root_trace.payload["trace"]["id"],
        child_trace.payload["trace"]["id"],
        legacy_child_trace.payload["trace"]["id"],
    }
    assert scope.unassigned_trace_ids == (unassigned_trace.payload["trace"]["id"],)
    assert set(scope.asset_ids) == {
        "root_input_asset",
        "child_input_asset",
        "duplicate-seen",
        "produced",
    }
    assert "duplicate-unseen" not in scope.asset_ids
    assert scope.produced_asset_ids == ("produced",)
