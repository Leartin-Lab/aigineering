"""Single-assignment terminal facts across stores and replicas."""

from collections import deque as _stdlib_deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import ast
import json
from pathlib import Path

import pytest

from conftest import candidate_runtime
import aigineering.core.fact_reducer as fact_reducer_module
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.fact_reducer import FactReducer
from aigineering.core.fact_materialization import materialize_fact_reduction
from aigineering.core.ids import contract_identity_v3
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset, Contract


def _legacy_fact_reduction(reducer, assets, *, pending_contracts=()):
    """Unindexed dispatch reference matching the pre-D1b2 batch loops."""
    events = []
    pending_names = {asset.name for asset in assets}
    contracts = reducer._contracts_with(tuple(pending_contracts))
    terminal_contract_ids = {
        str(record.payload.get("contract_id", ""))
        for _, record in reducer._store.scan_runtime_records(
            record_type="lifecycle.terminal"
        )
    }
    for asset in assets:
        events.extend(reducer._detect_method_result(asset, contracts))
    available_names = (
        {asset.name for asset in reducer._store.get_all_assets()} if assets else set()
    )
    available_names.update(pending_names)
    activated = set()
    for asset in assets:
        for event in reducer._detect_activation(asset, available_names, contracts):
            if event.contract_id not in activated:
                events.append(event)
                activated.add(event.contract_id)
    completed = set()
    for asset in assets:
        events.extend(
            reducer._detect_output_satisfaction(
                asset, tuple(assets), completed, terminal_contract_ids, contracts
            )
        )
    deduplicated = []
    cancelled = set()
    for event in events:
        if event.type == "child_cancelled":
            if event.contract_id in cancelled:
                continue
            cancelled.add(event.contract_id)
        deduplicated.append(event)
    return deduplicated


def _materialized_ids(events, assets):
    traces, records = materialize_fact_reduction(events, assets)
    return tuple(entry.id for entry in traces), tuple(record.id for record in records)


def _install_fact_reducer_observer(monkeypatch, store):
    """Observe real reducer calls while separately computing the old reference."""
    state = {"windows": [], "active": False, "stage": "idle"}
    original_on_assets = FactReducer.on_assets_created
    original_builder = fact_reducer_module._build_asset_dispatch_index
    original_capture = FactReducer._contracts_with
    original_method = FactReducer._detect_method_result
    original_activation = FactReducer._detect_activation
    original_output = FactReducer._detect_output_satisfaction
    original_graph = FactReducer._detect_unfinished_children
    original_check = fact_reducer_module.check_activation
    original_output_helper = fact_reducer_module.all_outputs_satisfied
    original_business_output = fact_reducer_module.is_business_output
    original_get_assets = store.get_all_assets
    original_get_assets_by_name = store.get_assets_by_name
    original_get_contracts = store.get_all_contracts
    original_get_contract = store.get_contract
    original_scan = store.scan_runtime_records

    class CountingContracts:
        def __init__(self, source):
            self.source = source
            self.yields = 0

        def __iter__(self):
            for contract in self.source:
                self.yields += 1
                yield contract

    def counted_builder(contracts, assets):
        if not state["active"]:
            return original_builder(contracts, assets)
        counter = CountingContracts(contracts)
        previous_stage = state["stage"]
        state["stage"] = "dispatch_index_build"
        try:
            result = original_builder(counter, assets)
        finally:
            state["stage"] = previous_stage
        state.setdefault("index_yields", []).append(counter.yields)
        return result

    def observed_capture(self, pending_contracts):
        if not state["active"]:
            return original_capture(self, pending_contracts)
        previous_stage = state["stage"]
        state["stage"] = "contracts_with_capture_and_sort"
        try:
            contracts = original_capture(self, pending_contracts)
        finally:
            state["stage"] = previous_stage
        state.setdefault("contract_captures", []).append(
            {
                "committed_rows": state.get("contract_read_rows", [])[-1],
                "pending_rows": len(pending_contracts),
                "overlay_rows": len(contracts),
                "ordered_ids": tuple(contract.id for contract in contracts),
            }
        )
        state["current_contracts"] = contracts
        return contracts

    class CountingOwnerMap:
        def __init__(self, source):
            self.source = source

        def get(self, key, default=None):
            state["method_owner_lookups"] = state.get("method_owner_lookups", 0) + 1
            return self.source.get(key, default)

    def observed_method(self, asset, contracts, *, contract_by_id=None):
        if not state["active"]:
            return original_method(
                self, asset, contracts, contract_by_id=contract_by_id
            )
        previous_stage = state["stage"]
        state["stage"] = "method_dispatch"
        try:
            owners = (
                CountingOwnerMap(contract_by_id) if contract_by_id is not None else None
            )
            return original_method(self, asset, contracts, contract_by_id=owners)
        finally:
            state["stage"] = previous_stage

    def observed_activation(
        self,
        asset,
        available_names,
        contracts,
        *,
        candidate_contracts=None,
        activation_results=None,
    ):
        if state["active"]:
            state.setdefault("activation_candidates", []).append(
                (len(contracts), len(candidate_contracts or ()))
            )
        previous_stage = state["stage"]
        if state["active"]:
            state["stage"] = "activation_dispatch"
        try:
            return original_activation(
                self,
                asset,
                available_names,
                contracts,
                candidate_contracts=candidate_contracts,
                activation_results=activation_results,
            )
        finally:
            state["stage"] = previous_stage

    def observed_output(
        self,
        asset,
        pending_assets,
        completed,
        terminal_contract_ids,
        contracts,
        *,
        candidate_contracts=None,
    ):
        if state["active"]:
            state.setdefault("output_candidates", []).append(
                (len(contracts), len(candidate_contracts or ()))
            )
        previous_stage = state["stage"]
        if state["active"]:
            state["stage"] = "output_dispatch"
        try:
            return original_output(
                self,
                asset,
                pending_assets,
                completed,
                terminal_contract_ids,
                contracts,
                candidate_contracts=candidate_contracts,
            )
        finally:
            state["stage"] = previous_stage

    def observed_graph(
        self, completed_contract, pending_assets, terminal_contract_ids, contracts
    ):
        if not state["active"]:
            return original_graph(
                self,
                completed_contract,
                pending_assets,
                terminal_contract_ids,
                contracts,
            )
        previous_stage = state["stage"]
        state["stage"] = "descendant_graph"
        counter = CountingContracts(contracts)
        try:
            result = original_graph(
                self, completed_contract, pending_assets, terminal_contract_ids, counter
            )
        finally:
            state["stage"] = previous_stage
        state.setdefault("graph_calls", []).append(1)
        state.setdefault("graph_contract_yields", []).append(counter.yields)
        return result

    def counted_check(expression, available_names):
        if state["active"]:
            state["parser_calls"] = state.get("parser_calls", 0) + 1
        return original_check(expression, available_names)

    def counted_output_helper(*args, **kwargs):
        if not state["active"]:
            return original_output_helper(*args, **kwargs)
        state["output_helper_calls"] = state.get("output_helper_calls", 0) + 1
        previous_stage = state["stage"]
        state["stage"] = "shared_output_helper"
        try:
            return original_output_helper(*args, **kwargs)
        finally:
            state["stage"] = previous_stage

    def counted_business_output(*args, **kwargs):
        if state["active"]:
            state["business_output_checks"] = state.get("business_output_checks", 0) + 1
        return original_business_output(*args, **kwargs)

    def counted_get_assets():
        result = original_get_assets()
        if state["active"]:
            state.setdefault("store_reads", []).append(
                {"stage": state["stage"], "kind": "assets", "rows": len(result)}
            )
        return result

    def counted_get_assets_by_name(name):
        result = original_get_assets_by_name(name)
        if state["active"]:
            state.setdefault("store_reads", []).append(
                {
                    "stage": state["stage"],
                    "kind": "assets_by_name",
                    "key": name,
                    "rows": len(result),
                }
            )
        return result

    def counted_get_contracts():
        result = original_get_contracts()
        if state["active"]:
            state.setdefault("contract_read_rows", []).append(len(result))
            state.setdefault("store_reads", []).append(
                {
                    "stage": state["stage"],
                    "kind": "contracts",
                    "rows": len(result),
                }
            )
        return result

    def counted_get_contract(contract_id):
        result = original_get_contract(contract_id)
        if state["active"]:
            state.setdefault("store_reads", []).append(
                {
                    "stage": state["stage"],
                    "kind": "contract_by_id",
                    "key": contract_id,
                    "rows": int(result is not None),
                }
            )
        return result

    def counted_scan(*, after_revision=0, through_revision=None, record_type=None):
        result = original_scan(
            after_revision=after_revision,
            through_revision=through_revision,
            record_type=record_type,
        )
        if state["active"]:
            state.setdefault("store_reads", []).append(
                {
                    "stage": state["stage"],
                    "kind": f"runtime_records:{record_type or '*'}",
                    "rows": len(result),
                }
            )
        return result

    def observed_on_assets(self, assets, *, pending_contracts=()):
        assets = tuple(assets)
        state["active"] = True
        before_yields = len(state.get("index_yields", ()))
        before_captures = len(state.get("contract_captures", ()))
        before_activation = len(state.get("activation_candidates", ()))
        before_output = len(state.get("output_candidates", ()))
        before_parser = state.get("parser_calls", 0)
        before_method_lookups = state.get("method_owner_lookups", 0)
        before_graph_calls = len(state.get("graph_calls", ()))
        before_graph_yields = len(state.get("graph_contract_yields", ()))
        before_helpers = state.get("output_helper_calls", 0)
        before_business_checks = state.get("business_output_checks", 0)
        before_reads = len(state.get("store_reads", ()))
        state["stage"] = "fact_reducer"
        actual = original_on_assets(self, assets, pending_contracts=pending_contracts)
        state["stage"] = "idle"
        state["active"] = False
        expected = _legacy_fact_reduction(
            self, assets, pending_contracts=pending_contracts
        )
        assert actual == expected
        assert _materialized_ids(actual, assets) == _materialized_ids(expected, assets)
        contracts = state.get("current_contracts", ())
        l_total = 0
        fallback_count = 0
        for contract in contracts:
            activation = contract.activation
            if isinstance(activation, str):
                l_total += len(activation) + len(activation.split())
            elif activation:
                fallback_count += 1
            l_total += sum(1 + len(output_name) for output_name in contract.outputs)
        activation_candidates = tuple(
            state.get("activation_candidates", ())[before_activation:]
        )
        output_candidates = tuple(state.get("output_candidates", ())[before_output:])
        method_lookups = state.get("method_owner_lookups", 0) - before_method_lookups
        activation_k = sum(
            candidate_count for _, candidate_count in activation_candidates
        )
        output_k = sum(candidate_count for _, candidate_count in output_candidates)
        state["windows"].append(
            {
                "assets": len(assets),
                "contracts": len(contracts),
                "index_yields": tuple(state.get("index_yields", ())[before_yields:]),
                "contract_capture_sort": state.get("contract_captures", ())[
                    before_captures:
                ],
                "activation_candidates": activation_candidates,
                "output_candidates": output_candidates,
                "parser_calls": state.get("parser_calls", 0) - before_parser,
                "L": l_total,
                "activation_fallback_count": fallback_count,
                "K_activation": activation_k,
                "K_output": output_k,
                "K_method_owner_lookups": method_lookups,
                "K_total": activation_k + output_k + method_lookups,
                "output_helper_calls": state.get("output_helper_calls", 0)
                - before_helpers,
                "business_output_checks": state.get("business_output_checks", 0)
                - before_business_checks,
                "graph_calls": len(state.get("graph_calls", ())) - before_graph_calls,
                "graph_contract_yields": tuple(
                    state.get("graph_contract_yields", ())[before_graph_yields:]
                ),
                "store_reads": state.get("store_reads", ())[before_reads:],
                "events": tuple(
                    (event.type, event.contract_id, event.asset_name)
                    for event in actual
                ),
                "materialized_ids": _materialized_ids(actual, assets),
            }
        )
        return actual

    monkeypatch.setattr(
        fact_reducer_module, "_build_asset_dispatch_index", counted_builder
    )
    monkeypatch.setattr(FactReducer, "_contracts_with", observed_capture)
    monkeypatch.setattr(FactReducer, "_detect_method_result", observed_method)
    monkeypatch.setattr(fact_reducer_module, "check_activation", counted_check)
    monkeypatch.setattr(
        fact_reducer_module, "all_outputs_satisfied", counted_output_helper
    )
    monkeypatch.setattr(
        fact_reducer_module, "is_business_output", counted_business_output
    )
    monkeypatch.setattr(FactReducer, "_detect_activation", observed_activation)
    monkeypatch.setattr(FactReducer, "_detect_output_satisfaction", observed_output)
    monkeypatch.setattr(FactReducer, "_detect_unfinished_children", observed_graph)
    monkeypatch.setattr(FactReducer, "on_assets_created", observed_on_assets)
    monkeypatch.setattr(store, "get_all_assets", counted_get_assets)
    monkeypatch.setattr(store, "get_assets_by_name", counted_get_assets_by_name)
    monkeypatch.setattr(store, "get_all_contracts", counted_get_contracts)
    monkeypatch.setattr(store, "get_contract", counted_get_contract)
    monkeypatch.setattr(store, "scan_runtime_records", counted_scan)
    return state


def _signed_dispatch_fixture(runtime, history_count, matched_count):
    """Create H signed closed Contracts and a fixed 32-Contract dispatch set."""
    for index in range(history_count):
        name = f"d1b2-history-{index}"
        contract = runtime.accept_contract(
            build_control_plane_contract(
                name=name, outputs=(name,), activation="", budget=1
            )
        )
        runtime.accept_asset(
            Asset(
                id="",
                name=name,
                content="closed history",
                origin="human",
                created_by=contract.id,
            )
        )

    contracts = []
    for index in range(32):
        name = f"d1b2-probe-{index}"
        outputs = (
            (name, f"d1b2-missing-{index}")
            if index < matched_count
            else (f"d1b2-decoy-{index}",)
        )
        activation = (
            f"{name} AND d1b2-absent-{index}" if index < matched_count else "never"
        )
        contracts.append(
            runtime.accept_contract(
                build_control_plane_contract(
                    name=f"dispatch-{index}",
                    outputs=outputs,
                    activation=activation,
                    budget=1,
                )
            )
        )
    assets = tuple(
        Asset(
            id="",
            name=f"d1b2-probe-{index}",
            content="measured batch",
            origin="human",
            created_by=contracts[index].id,
        )
        for index in range(matched_count)
    )
    return tuple(contracts), assets


@pytest.mark.parametrize("reverse", [False, True])
def test_dispatch_reference_covers_pending_overlay_and_same_name_multivalues(
    monkeypatch, record_property, reverse
):
    store = MemoryStore()
    committed = Contract(
        id="overlay",
        activation="stale",
        outputs=("stale",),
    )
    store.add_contract(committed)
    replacement = Contract(
        id="overlay",
        activation="shared OR second",
        outputs=("shared", "shared", "missing-overlay"),
    )
    pending = Contract(
        id="pending-new",
        activation="shared OR second",
        outputs=("shared", "missing-new"),
    )
    same_name = (
        Asset(id="observation", name="shared", content="observed", origin="tool"),
        Asset(id="business", name="shared", content="accepted", origin="human"),
    )
    assets = (*same_name, Asset(id="second", name="second", content="accepted"))
    if reverse:
        assets = tuple(reversed(assets))

    state = _install_fact_reducer_observer(monkeypatch, store)
    events = FactReducer(store).on_assets_created(
        assets, pending_contracts=(replacement, pending)
    )

    assert len(state["windows"]) == 1
    snapshot = state["windows"][0]
    record_property("overlay_same_name_measurement", json.dumps(snapshot))
    assert snapshot["contracts"] == 2
    assert snapshot["contract_capture_sort"][0]["pending_rows"] == 2
    assert snapshot["contract_capture_sort"][0]["committed_rows"] == 1
    assert snapshot["K_activation"] == 6
    assert snapshot["K_output"] == 4
    assert snapshot["parser_calls"] == 2
    activation_events = [event for event in events if event.type == "activation_active"]
    assert [(event.contract_id, event.asset_name) for event in activation_events] == [
        ("overlay", assets[0].name),
        ("pending-new", assets[0].name),
    ]
    output_events = [event for event in events if event.type == "output_satisfied"]
    assert [(event.contract_id, event.asset_name) for event in output_events] == [
        ("overlay", "shared"),
        ("pending-new", "shared"),
    ]
    assert not any(
        event.contract_id == "overlay" and event.asset_name == "stale"
        for event in events
    )
    assert not any(event.type == "contract_complete" for event in events)


@pytest.mark.parametrize("reverse", [False, True])
def test_dispatch_reference_preserves_first_asset_and_memoizes_activation(
    monkeypatch, record_property, reverse
):
    store = MemoryStore()
    store.add_contract(Contract(id="multi-match", activation="left OR right"))
    assets = (
        Asset(id="left", name="left", content="value"),
        Asset(id="right", name="right", content="value"),
    )
    if reverse:
        assets = tuple(reversed(assets))
    state = _install_fact_reducer_observer(monkeypatch, store)

    events = FactReducer(store).on_assets_created(assets)

    assert [(event.contract_id, event.asset_name) for event in events] == [
        ("multi-match", assets[0].name)
    ]
    assert state["windows"][0]["K_activation"] == 2
    assert state["windows"][0]["parser_calls"] == 1
    record_property("first_activation_measurement", json.dumps(state["windows"][0]))


def test_dispatch_reference_checks_all_method_prefix_owner_cases(
    monkeypatch, record_property
):
    store = MemoryStore()
    exact_owners = []
    assets = []
    for index, prefix in enumerate(fact_reducer_module.METHOD_RESULT_PREFIXES):
        name = f"{prefix}exact-{index}"
        owner = Contract(id=f"method-owner-{index}", outputs=(name,))
        store.add_contract(owner)
        exact_owners.append(owner)
        assets.append(
            Asset(
                id=f"method-asset-{index}",
                name=name,
                content="value",
                created_by=owner.id,
            )
        )
    terminal_owner = exact_owners[-1]
    store.append_runtime_record(create_terminal_record(terminal_owner.id, "complete"))

    missing_name = f"{fact_reducer_module.METHOD_RESULT_PREFIXES[0]}missing"
    assets.append(Asset(id="missing", name=missing_name, content="value"))
    wrong_name = f"{fact_reducer_module.METHOD_RESULT_PREFIXES[1]}wrong-owner"
    assets.append(
        Asset(
            id="wrong-owner",
            name=wrong_name,
            content="value",
            created_by=exact_owners[0].id,
        )
    )
    undeclared_name = f"{fact_reducer_module.METHOD_RESULT_PREFIXES[2]}undeclared"
    undeclaring_owner = Contract(id="method-owner-non-declaring", outputs=("other",))
    store.add_contract(undeclaring_owner)
    assets.append(
        Asset(
            id="non-declaring",
            name=undeclared_name,
            content="value",
            created_by=undeclaring_owner.id,
        )
    )
    state = _install_fact_reducer_observer(monkeypatch, store)

    events = FactReducer(store).on_assets_created(tuple(assets))

    method_events = [
        event for event in events if event.type == "method_result_detected"
    ]
    assert [event.contract_id for event in method_events] == [
        owner.id for owner in exact_owners
    ]
    assert [event.details["prefix"] for event in method_events] == list(
        fact_reducer_module.METHOD_RESULT_PREFIXES
    )
    assert state["windows"][0]["K_method_owner_lookups"] == len(assets)
    assert state["windows"][0]["output_helper_calls"] == len(exact_owners) - 1
    assert state["windows"][0]["graph_calls"] == len(exact_owners) - 1
    assert state["windows"][0]["K_total"] == len(assets) + len(exact_owners)
    assert state["windows"][0]["graph_contract_yields"] == tuple(
        [2 * len(store.get_all_contracts())] * (len(exact_owners) - 1)
    )
    record_property("method_owner_measurement", json.dumps(state["windows"][0]))


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_contract_terminal_is_single_assignment_and_exact_replay_is_idempotent(kind):
    store = MemoryStore() if kind == "memory" else SQLiteStore(":memory:")
    store.add_contract(Contract(id="task:one", name="one"))
    complete = create_runtime_record(
        "lifecycle.terminal", {"contract_id": "task:one", "terminal": "complete"}
    )
    failed = create_runtime_record(
        "lifecycle.terminal", {"contract_id": "task:one", "terminal": "failed"}
    )

    revision = store.append_runtime_record(complete)
    assert store.append_runtime_record(complete) == revision
    with pytest.raises(ImmutableRecordConflict, match="contract terminal"):
        store.append_runtime_record(failed)

    if isinstance(store, SQLiteStore):
        store.close()


def test_terminal_factory_preserves_optional_causal_fields():
    terminal = create_terminal_record(
        "task:factory",
        "cancelled",
        actor_id="human:owner",
        reason="superseded",
        causal_parents=("record:cause",),
        recorded_at="2026-08-09T00:00:00+00:00",
    )

    assert terminal.payload == {
        "actor_id": "human:owner",
        "contract_id": "task:factory",
        "reason": "superseded",
        "terminal": "cancelled",
    }
    assert terminal.causal_parents == ("record:cause",)
    assert terminal.recorded_at == "2026-08-09T00:00:00+00:00"


def test_lifecycle_module_is_the_only_terminal_record_constructor():
    package_root = Path(fact_reducer_module.__file__).resolve().parents[1]
    owner = package_root / "core" / "lifecycle_facts.py"
    violations = []
    for path in package_root.rglob("*.py"):
        if path == owner:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name != "create_runtime_record":
                continue
            record_type = node.args[0] if node.args else None
            if isinstance(record_type, ast.Constant) and record_type.value == (
                "lifecycle.terminal"
            ):
                violations.append(f"{path}:{node.lineno}")
    assert violations == []


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_non_terminal_insert_does_not_scan_terminal_history(kind, monkeypatch):
    store = MemoryStore() if kind == "memory" else SQLiteStore(":memory:")
    original_scan = store.scan_runtime_records

    def guarded_scan(*, after_revision=0, record_type=None):
        if record_type == "lifecycle.terminal":
            raise AssertionError("non-terminal append scanned terminal history")
        return original_scan(after_revision=after_revision, record_type=record_type)

    monkeypatch.setattr(store, "scan_runtime_records", guarded_scan)
    record = create_runtime_record("test.observed", {"value": "ok"})

    assert store.append_runtime_record(record) == 1
    assert store.append_runtime_record(record) == 1

    if isinstance(store, SQLiteStore):
        store.close()


def test_sqlite_terminal_uniqueness_arbitrates_competing_replicas(tmp_path):
    path = str(tmp_path / "terminal.db")
    initial = SQLiteStore(path)
    initial.add_contract(Contract(id="task:shared", name="shared"))
    initial.close()
    records = (
        create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": "task:shared", "terminal": "complete"},
        ),
        create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": "task:shared", "terminal": "failed"},
        ),
    )

    def append(record):
        store = SQLiteStore(path)
        try:
            store.append_runtime_record(record)
            return "committed"
        except ImmutableRecordConflict:
            return "conflict"
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, records))

    assert sorted(results) == ["committed", "conflict"]
    reopened = SQLiteStore(path)
    assert len(reopened.scan_runtime_records(record_type="lifecycle.terminal")) == 1
    reopened.close()


def test_sqlite_terminal_atomically_releases_active_claim_and_rebuilds():
    store = SQLiteStore(":memory:")
    contract = Contract(id="task:claimed", name="claimed")
    store.add_contract(contract)
    claim = store.claim_contract(
        contract.id,
        "worker:late",
        package_id="package:late",
    )
    assert claim is not None and claim["status"] == "active"
    terminal = create_runtime_record(
        "lifecycle.terminal",
        {"contract_id": contract.id, "terminal": "cancelled"},
    )

    store.commit_ingress_batch(
        accepted_assets=[],
        trace_entries=[],
        runtime_records=(terminal,),
    )

    released = store.get_claim(contract.id)
    assert released is not None and released["status"] == "released"
    release_records = store.scan_runtime_records(record_type="claim.released")
    assert len(release_records) == 1
    assert terminal.id in release_records[0][1].causal_parents

    store.rebuild_claim_projection()
    rebuilt = store.get_claim(contract.id)
    assert rebuilt is not None and rebuilt["status"] == "released"
    assert store.claim_contract(contract.id, "worker:new") is None
    store.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_terminal_fact_cannot_precede_its_contract(kind):
    store = MemoryStore() if kind == "memory" else SQLiteStore(":memory:")
    terminal = create_runtime_record(
        "lifecycle.terminal",
        {"contract_id": "task:missing", "terminal": "cancelled"},
    )

    with pytest.raises(ValueError, match="unknown Contract"):
        store.append_runtime_record(terminal)

    if isinstance(store, SQLiteStore):
        store.close()


def test_atomic_fact_batch_does_not_cancel_child_satisfied_by_same_batch():
    store = MemoryStore()
    parent = build_control_plane_contract(
        name="parent", outputs=("shared_result",), budget=2
    )
    child = replace(
        build_control_plane_contract(
            name="child", outputs=("shared_result",), budget=1
        ),
        parent_id=parent.id,
    )
    child = replace(child, id=contract_identity_v3(child))
    store.add_contract(parent)
    store.add_contract(child)
    asset = Asset(
        id="asset:shared",
        name="shared_result",
        content="done",
        created_by=child.id,
        content_hash="hash",
    )

    events = FactReducer(store, MemoryTraceStore()).on_assets_created((asset,))

    terminals = [(event.type, event.contract_id) for event in events]
    assert ("contract_complete", parent.id) in terminals
    assert ("contract_complete", child.id) in terminals
    assert ("child_cancelled", child.id) not in terminals


@pytest.mark.parametrize("sibling_count", [8, 16])
def test_signed_wide_sibling_completion_uses_fifo_linear_queue_operations(
    sibling_count, monkeypatch
):
    """Count the production queue over both projection windows of a real commit."""
    from aigineering.protocol.effect_builders import asset_proposal_effect

    counts = {"append": 0, "popleft": 0, "instances": 0}

    class CountingDeque:
        def __init__(self, values):
            counts["instances"] += 1
            self.items = _stdlib_deque(values)

        def append(self, value):
            counts["append"] += 1
            self.items.append(value)

        def popleft(self):
            counts["popleft"] += 1
            return self.items.popleft()

        def __bool__(self):
            return bool(self.items)

    # raising=False lets the old implementation execute its signed publication;
    # zero observations then fail the exact-count assertions below.
    monkeypatch.setattr(
        fact_reducer_module,
        "deque",
        lambda values=(): CountingDeque(values),
        raising=False,
    )
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    root = runtime.accept_contract(
        build_control_plane_contract(
            name=f"wide-root-{sibling_count}",
            outputs=("wide-root-result",),
            budget=sibling_count + 2,
        )
    )
    children = []
    for index in range(sibling_count):
        child = replace(
            build_control_plane_contract(
                name=f"wide-child-{sibling_count}-{index:02d}",
                outputs=(f"wide-child-result-{index:02d}",),
                budget=1,
            ),
            parent_id=root.id,
            id="",
        )
        child = replace(child, id=contract_identity_v3(child))
        children.append(runtime.accept_contract(child))
    children = tuple(children)

    reducer_events = []
    original_detect = fact_reducer_module.FactReducer._detect_unfinished_children

    def capture_detect(self, completed_contract, *args, **kwargs):
        events = original_detect(self, completed_contract, *args, **kwargs)
        if completed_contract.id == root.id:
            reducer_events.append(events)
        return events

    monkeypatch.setattr(
        fact_reducer_module.FactReducer,
        "_detect_unfinished_children",
        capture_detect,
    )
    decision = runtime.publisher.publish(
        (
            asset_proposal_effect(
                Asset(
                    id="",
                    name="wide-root-result",
                    content="root completed",
                    origin="human",
                )
            ),
        ),
        idempotency_key=f"complete-wide-root-{sibling_count}",
    )

    expected_ids = sorted(child.id for child in children)
    assert decision.accepted
    assert len(reducer_events) == 2
    for events in reducer_events:
        assert [event.contract_id for event in events] == expected_ids
        assert all(event.type == "child_cancelled" for event in events)
        assert all(event.details["parent_id"] == root.id for event in events)
    assert counts == {
        "append": 2 * sibling_count,
        "popleft": 2 * (sibling_count + 1),
        "instances": 2,
    }
    reducer_tree = ast.parse(
        Path(fact_reducer_module.__file__).resolve().read_text(encoding="utf-8")
    )
    reducer_class = next(
        node
        for node in reducer_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "FactReducer"
    )
    traversal = next(
        node
        for node in reducer_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_detect_unfinished_children"
    )
    calls = [node for node in ast.walk(traversal) if isinstance(node, ast.Call)]
    assert any(
        isinstance(node.func, ast.Attribute) and node.func.attr == "popleft"
        for node in calls
    )
    assert not any(
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "pop"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == 0
        for node in calls
    )

    terminals = {
        record.payload["contract_id"]: record
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    }
    assert terminals[root.id].payload["terminal"] == "complete"
    assert [
        contract_id
        for contract_id in expected_ids
        if terminals[contract_id].payload["terminal"] == "cancelled"
    ] == expected_ids
    for contract_id in expected_ids:
        trace = next(
            event
            for event in store.get_trace_events(contract_id)
            if event.event_type == "cancelled"
        )
        assert trace.relation_type == "unreachable"
        assert trace.relation_target == root.id
        assert trace.rejected_fragments == (
            f"[unreachable] parent_complete: parent {root.id} completed",
        )
    store.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_bad_pending_child_output_does_not_evade_cancellation_fence(reverse):
    store = MemoryStore()
    parent = Contract(id="parent", name="parent", outputs=("parent_result",))
    child = Contract(
        id="child",
        name="child",
        parent_id=parent.id,
        outputs=("child_result",),
    )
    store.add_contract(parent)
    store.add_contract(child)
    parent_output = Asset(
        id="parent-output",
        name="parent_result",
        content="done",
        created_by=parent.id,
        origin="human",
    )
    child_observation = Asset(
        id="child-observation",
        name="child_result",
        content="observed",
        created_by=child.id,
        origin="tool",
    )
    assets = (
        (child_observation, parent_output)
        if reverse
        else (parent_output, child_observation)
    )

    events = FactReducer(store, MemoryTraceStore()).on_assets_created(assets)

    terminals = {(event.type, event.contract_id) for event in events}
    assert ("contract_complete", parent.id) in terminals
    assert ("child_cancelled", child.id) in terminals


def test_completed_planning_stage_keeps_ancestor_acceptance_verifier_reachable():
    store = MemoryStore()
    root = replace(
        build_control_plane_contract(name="root", outputs=("report",), budget=8),
        acceptance_policy={
            "mode": "independent",
            "policy_version": "review-v1",
            "required_attestations": 1,
            "verifier_capabilities": ("report.verify",),
        },
    )
    root = replace(root, id=contract_identity_v3(root))
    compiler = replace(
        build_control_plane_contract(name="compiler", outputs=("report",), budget=6),
        parent_id=root.id,
    )
    compiler = replace(compiler, id=contract_identity_v3(compiler))
    verifier = replace(
        build_control_plane_contract(name="verifier", outputs=("receipt",), budget=1),
        parent_id=compiler.id,
        inputs=("report",),
        worker_capabilities=("report.verify",),
    )
    verifier = replace(verifier, id=contract_identity_v3(verifier))
    for contract in (root, compiler, verifier):
        store.add_contract(contract)
    report = Asset(
        id="asset:report",
        name="report",
        content="result",
        created_by=compiler.id,
        content_hash="report-hash",
    )

    events = FactReducer(store, MemoryTraceStore()).on_assets_created((report,))

    terminals = [(event.type, event.contract_id) for event in events]
    assert ("contract_complete", compiler.id) in terminals
    assert ("contract_complete", root.id) not in terminals
    assert ("child_cancelled", verifier.id) not in terminals


def test_fact_reducer_does_not_repeat_a_recorded_terminal():
    store = MemoryStore()
    contract = build_control_plane_contract(
        name="finished", outputs=("result",), budget=1
    )
    store.add_contract(contract)
    store.append_runtime_record(
        create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": contract.id, "terminal": "complete"},
        )
    )
    asset = Asset(
        id="asset:later",
        name="result",
        content="later observation",
        created_by=contract.id,
        content_hash="hash",
    )

    events = FactReducer(store, MemoryTraceStore()).on_assets_created((asset,))

    assert not [event for event in events if event.contract_id == contract.id]


def test_method_result_event_requires_declaring_contract_provenance():
    store = MemoryStore()
    result_name = "_plan_result_parent"
    plan_task = Contract(
        id="plan-task",
        name="parent.plan",
        outputs=(result_name,),
        origin="system",
    )
    store.add_contract(plan_task)
    forged = Asset(
        id="forged-plan",
        name=result_name,
        content='{"contracts": []}',
        created_by="plugin:planning",
    )
    authentic = replace(forged, id="authentic-plan", created_by=plan_task.id)
    reducer = FactReducer(store, MemoryTraceStore())

    assert not [
        event
        for event in reducer.on_assets_created((forged,))
        if event.type == "method_result_detected"
    ]
    detected = [
        event
        for event in reducer.on_assets_created((authentic,))
        if event.type == "method_result_detected"
    ]
    assert [(event.contract_id, event.asset_name) for event in detected] == [
        (plan_task.id, result_name)
    ]


@pytest.mark.parametrize("completion_entry", ["output", "qualification"])
def test_completion_descendant_projection_is_shared_and_read_only(
    completion_entry, monkeypatch
):
    store = MemoryStore()
    root = Contract(id="root", name="root", outputs=("root-result",))
    intermediate = Contract(id="intermediate", name="intermediate", parent_id=root.id)
    leaf = Contract(
        id="leaf", name="leaf", parent_id=intermediate.id, outputs=("leaf-result",)
    )
    for contract in (root, intermediate, leaf):
        store.add_contract(contract)
    terminal = create_terminal_record(intermediate.id, "cancelled")
    store.append_runtime_record(terminal)
    original_records = store.scan_runtime_records()

    def no_mutation(*args, **kwargs):
        raise AssertionError("completion projection attempted a durable mutation")

    monkeypatch.setattr(store, "append_runtime_record", no_mutation)
    monkeypatch.setattr(store, "add_asset", no_mutation)
    monkeypatch.setattr(store, "add_contract", no_mutation)
    reducer = FactReducer(store)
    if completion_entry == "output":
        events = reducer.on_asset_created(
            Asset(id="result", name="root-result", content="done", created_by=root.id)
        )
    else:
        events = reducer.on_contract_completed(
            root, terminal_contract_ids={root.id, intermediate.id}
        )
    cancelled = [event for event in events if event.type == "child_cancelled"]
    assert [event.contract_id for event in cancelled] == [leaf.id]
    assert cancelled[0].details["parent_id"] == intermediate.id
    assert store.scan_runtime_records() == original_records


@pytest.mark.parametrize(
    ("auxiliary_origin", "expected_complete"),
    [("tool", False), ("mcp", False), ("label_placeholder", False), ("human", True)],
)
def test_signed_same_batch_outputs_retain_provenance_through_reopen_and_rebuild(
    tmp_path, auxiliary_origin, expected_complete
):
    path = str(tmp_path / f"pending-output-{auxiliary_origin}.db")
    store = SQLiteStore(path)
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(
        build_control_plane_contract(
            name=f"pending-output-{auxiliary_origin}",
            inputs=(),
            outputs=("business_result", "auxiliary_result"),
            activation="",
            budget=1,
        )
    )
    decision = runtime.publisher.publish(
        (
            asset_proposal_effect(
                Asset(
                    id="",
                    name="business_result",
                    content="business output",
                    origin="human",
                )
            ),
            asset_proposal_effect(
                Asset(
                    id="",
                    name="auxiliary_result",
                    content="auxiliary observation",
                    origin=auxiliary_origin,
                )
            ),
        ),
        idempotency_key=f"pending-output-{auxiliary_origin}",
    )

    assert decision.accepted
    assert all_outputs_satisfied(contract, store) is expected_complete
    terminals = [
        record.payload.get("terminal")
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("contract_id") == contract.id
    ]
    assert terminals == (["complete"] if expected_complete else [])
    digest = store.rebuild_runtime_materializations(strict=True)
    store.close()

    reopened = SQLiteStore(path)
    try:
        assert reopened.rebuild_runtime_materializations(strict=True) == digest
        assert all_outputs_satisfied(contract, reopened) is expected_complete
        reopened_terminals = [
            record.payload.get("terminal")
            for _, record in reopened.scan_runtime_records(
                record_type="lifecycle.terminal"
            )
            if record.payload.get("contract_id") == contract.id
        ]
        assert reopened_terminals == (["complete"] if expected_complete else [])
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("history_count", "batch_size"),
    [(0, 1), (0, 32), (16, 8), (64, 1), (64, 32)],
)
def test_signed_asset_dispatch_index_has_one_pass_and_sparse_candidates(
    tmp_path, monkeypatch, record_property, history_count, batch_size
):
    path = str(tmp_path / f"d1b2-dispatch-{history_count}-{batch_size}.db")
    store = SQLiteStore(path)
    runtime = candidate_runtime(store)
    contracts, assets = _signed_dispatch_fixture(runtime, history_count, batch_size)
    total_contracts = history_count + len(contracts)
    assert len(store.get_all_contracts()) == total_contracts
    assert (
        len(
            [
                record
                for _, record in store.scan_runtime_records(
                    record_type="lifecycle.terminal"
                )
            ]
        )
        == history_count
    )

    state = _install_fact_reducer_observer(monkeypatch, store)
    decision = runtime.publisher.publish(
        tuple(asset_proposal_effect(asset) for asset in assets),
        idempotency_key=f"d1b2-dispatch-{history_count}-{batch_size}",
    )

    assert decision.accepted
    assert len(decision.assets) == batch_size
    assert len(state["windows"]) == 2
    for window in state["windows"]:
        assert window["contracts"] == total_contracts
        assert window["index_yields"] == (total_contracts,)
        capture = window["contract_capture_sort"][0]
        assert capture["committed_rows"] == total_contracts
        assert capture["pending_rows"] == 0
        assert capture["overlay_rows"] == total_contracts
        assert capture["ordered_ids"] == tuple(sorted(capture["ordered_ids"]))
        assert window["activation_candidates"] == tuple(
            (total_contracts, 1) for _ in range(batch_size)
        )
        assert window["output_candidates"] == tuple(
            (total_contracts, 1) for _ in range(batch_size)
        )
        assert window["parser_calls"] == batch_size
        assert window["L"] > 0
        assert window["K_activation"] == batch_size
        assert window["K_output"] == batch_size
        assert window["K_method_owner_lookups"] == 0
        assert window["K_total"] == 2 * batch_size
        assert window["output_helper_calls"] == batch_size
        helper_asset_reads = [
            read
            for read in window["store_reads"]
            if read["stage"] == "shared_output_helper"
            and read["kind"] == "assets_by_name"
        ]
        assert len(helper_asset_reads) == 2 * batch_size
        assert all(read["rows"] in {0, 1} for read in helper_asset_reads)
        assert window["graph_calls"] == 0
        assert window["graph_contract_yields"] == ()
        assert len(window["events"]) == batch_size
        assert all(event[0] == "output_satisfied" for event in window["events"])
    record_property("H", history_count)
    record_property("P", batch_size)
    record_property("C", total_contracts)
    record_property("two_window_dispatch_measurements", json.dumps(state["windows"]))
    assert not any(
        record.record_type == "candidate.rejected"
        for record in decision.runtime_records
    )
    target_ids = {contract.id for contract in contracts[:batch_size]}
    assert not any(
        record.payload.get("contract_id") in target_ids
        and record.record_type == "lifecycle.terminal"
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    )

    before_records = tuple(record.id for _, record in store.scan_runtime_records())
    before_traces = tuple(entry.id for entry in store.get_trace_events())
    digest = store.rebuild_runtime_materializations(strict=True)
    store.close()

    reopened = SQLiteStore(path)
    try:
        assert reopened.rebuild_runtime_materializations(strict=True) == digest
        assert (
            tuple(record.id for _, record in reopened.scan_runtime_records())
            == before_records
        )
        assert tuple(entry.id for entry in reopened.get_trace_events()) == before_traces
    finally:
        reopened.close()


def test_signed_completion_output_bucket_keeps_full_descendant_overlay(
    tmp_path, monkeypatch, record_property
):
    path = str(tmp_path / "d1b2-completion-overlay.db")
    store = SQLiteStore(path)
    runtime = candidate_runtime(store)
    root_template = replace(
        build_control_plane_contract(
            name="d1b2-independent-root",
            outputs=("d1b2-report",),
            budget=8,
        ),
        acceptance_policy={
            "mode": "independent",
            "policy_version": "review-v1",
            "required_attestations": 1,
            "verifier_capabilities": ("report.verify",),
        },
        id="",
    )
    root = runtime.accept_contract(root_template)
    compiler = runtime.accept_contract(
        replace(
            build_control_plane_contract(
                name="d1b2-compiler", outputs=("d1b2-report",), budget=6
            ),
            parent_id=root.id,
            id="",
        )
    )
    verifier = runtime.accept_contract(
        replace(
            build_control_plane_contract(
                name="d1b2-verifier", outputs=("d1b2-receipt",), budget=1
            ),
            parent_id=compiler.id,
            inputs=("d1b2-report",),
            worker_capabilities=("report.verify",),
            id="",
        )
    )
    unfinished = runtime.accept_contract(
        replace(
            build_control_plane_contract(
                name="d1b2-unfinished", outputs=("d1b2-unfinished-result",), budget=1
            ),
            parent_id=compiler.id,
            id="",
        )
    )
    assert all(
        "d1b2-report" not in contract.outputs for contract in (verifier, unfinished)
    )

    state = _install_fact_reducer_observer(monkeypatch, store)
    decision = runtime.publisher.publish(
        (
            asset_proposal_effect(
                Asset(
                    id="",
                    name="d1b2-report",
                    content="business result",
                    origin="human",
                    created_by=compiler.id,
                )
            ),
        ),
        idempotency_key="d1b2-completion-trigger",
    )
    assert decision.accepted
    assert len(state["windows"]) == 2
    for window in state["windows"]:
        events = window["events"]
        assert ("child_cancelled", unfinished.id, compiler.id) in events
        assert not any(
            event[0] == "child_cancelled" and event[1] == verifier.id
            for event in events
        )
        assert window["graph_calls"] == 1
        assert window["graph_contract_yields"] == (8,)
        assert window["output_helper_calls"] >= 2
        helper_reads = [
            read
            for read in window["store_reads"]
            if read["stage"] == "shared_output_helper"
        ]
        assert any(
            read["kind"] == "runtime_records:output.qualified" for read in helper_reads
        )
        assert any(read["kind"] == "assets_by_name" for read in helper_reads)
    record_property("completion_two_window_measurements", json.dumps(state["windows"]))
    terminal_ids = {
        record.payload.get("contract_id")
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("terminal") == "cancelled"
    }
    assert unfinished.id in terminal_ids
    assert verifier.id not in terminal_ids
    assert not any(
        record.record_type == "candidate.rejected"
        for record in decision.runtime_records
    )
    store.close()
