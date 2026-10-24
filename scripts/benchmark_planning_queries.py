#!/usr/bin/env python3
"""Measure signed planning publication against closed signed history.

The benchmark deliberately has two passes.  The timing pass has no tracing,
profiling, or method wrappers; the instrumentation pass repeats the same
case in a fresh Store and reports those overheads separately.
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import math
import os
import platform
import pstats
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import traceback
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class PublicationAttemptError(RuntimeError):
    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.publication_attempts = tuple(dict(item) for item in attempts)


def _load_package(*, installed: bool) -> None:
    if installed:
        if str(SRC) in sys.path:
            sys.path.remove(str(SRC))
    elif SRC.is_dir() and str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))


def _imports(*, installed: bool):
    _load_package(installed=installed)
    from aigineering import __version__
    from aigineering.agent.mock import MockWorker
    from aigineering.agent.worker import WorkerHost
    from aigineering.core.candidate_publisher import CandidatePublisher
    from aigineering.core.control_plane import (
        build_control_plane_asset,
        build_control_plane_contract,
    )
    from aigineering.core.domain import initialize_genesis
    from aigineering.core.signing import Ed25519Signer
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.core.worker_routing import WorkerRegistration
    from aigineering.plugins.planning import compile_planning_blueprint
    from aigineering.protocol.candidate import (
        ActorKey,
        create_genesis_manifest,
    )
    from aigineering.protocol.effect_builders import (
        asset_proposal_effect,
        contract_declaration_effect,
        worker_registration_effect,
    )
    from aigineering.protocol.wire import contract_from_dict
    from aigineering.runtime import claim_next_package, execute_claimed_package

    package_path = Path(__import__("aigineering").__file__).resolve()
    if installed and (SRC == package_path or SRC in package_path.parents):
        raise RuntimeError("--installed imported the source checkout")
    return {
        "__version__": __version__,
        "MockWorker": MockWorker,
        "WorkerHost": WorkerHost,
        "CandidatePublisher": CandidatePublisher,
        "build_control_plane_asset": build_control_plane_asset,
        "build_control_plane_contract": build_control_plane_contract,
        "initialize_genesis": initialize_genesis,
        "Ed25519Signer": Ed25519Signer,
        "SQLiteStore": SQLiteStore,
        "WorkerRegistration": WorkerRegistration,
        "compile_planning_blueprint": compile_planning_blueprint,
        "ActorKey": ActorKey,
        "create_genesis_manifest": create_genesis_manifest,
        "asset_proposal_effect": asset_proposal_effect,
        "contract_declaration_effect": contract_declaration_effect,
        "worker_registration_effect": worker_registration_effect,
        "contract_from_dict": contract_from_dict,
        "claim_next_package": claim_next_package,
        "execute_claimed_package": execute_claimed_package,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark signed planning publication over closed history."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sizes", nargs="+", type=int, default=[8, 16, 32, 64])
    parser.add_argument("--histories", nargs="+", type=int, default=[0, 16, 64])
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=("one_batch", "siblings_sequential", "chain"),
        default=["one_batch", "siblings_sequential", "chain"],
    )
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument(
        "--installed",
        action="store_true",
        help="import the installed package without adding this checkout's src/",
    )
    parser.add_argument(
        "--source-archive",
        action="store_true",
        help="use an externally supplied source archive on sys.path, without src/",
    )
    parser.add_argument(
        "--imported-source-revision",
        help="externally verified revision of the imported package, if known",
    )
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.output.exists():
        parser.error("refusing to overwrite existing output")
    if args.installed and args.source_archive:
        parser.error("--installed and --source-archive are mutually exclusive")
    if args.smoke:
        args.sizes, args.histories, args.modes, args.samples = (
            [2],
            [0, 2],
            ["one_batch", "siblings_sequential", "chain"],
            1,
        )
    if not args.sizes or any(n < 1 for n in args.sizes):
        parser.error("sizes must be positive integers")
    if not args.histories or any(n < 0 for n in args.histories):
        parser.error("histories must be non-negative integers")
    if args.samples < 1:
        parser.error("samples must be positive")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _metadata(args: argparse.Namespace, package_version: str) -> dict[str, Any]:
    package = __import__("aigineering")
    package_root = Path(next(iter(package.__path__))).resolve()
    package_hashes = {
        path.relative_to(package_root).as_posix(): _sha256(path)
        for path in sorted(package_root.rglob("*.py"))
        if path.is_file()
    }
    hashes: dict[str, Any] = {
        "benchmark_script": _sha256(Path(__file__)),
        "package_python_file_count": len(package_hashes),
        "package_python_files": package_hashes,
    }
    return {
        "benchmark": "aigineering-planning-query-growth",
        "schema_version": 1,
        "driver_checkout_revision": _git_revision(),
        "imported_source_revision": args.imported_source_revision,
        "import_mode": (
            "source_archive"
            if args.source_archive
            else "installed_artifact"
            if args.installed
            else "source_checkout"
        ),
        "package_version": package_version,
        "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "source_sha256": hashes,
        "parameters": {
            "sizes": list(args.sizes),
            "histories": list(args.histories),
            "modes": list(args.modes),
            "samples_per_cell": args.samples,
            "passes": ["timing", "instrumentation"],
        },
        "measurement_notes": {
            "complexity_dimensions": (
                "P is the number of newly declared child tasks; H is closed signed "
                "background contracts; A is the measured count of unclosed relevant "
                "contracts, including the benchmark parent. Unclosed does not imply ready."
            ),
            "vm_opcode_proxy": (
                "progress callbacks and callback interval product are coarse observed "
                "work proxies, neither a bound nor an exact opcode/row count"
            ),
            "materialized_rows": (
                "rows returned by the named public Store methods only; does not "
                "count every internal Python iteration"
            ),
            "timing_isolation": (
                "timing and instrumentation are separate runs in fresh Stores"
            ),
        },
    }


def _new_domain(db_path: Path, imports: dict[str, Any]):
    SQLiteStore = imports["SQLiteStore"]
    ActorKey = imports["ActorKey"]
    create_genesis_manifest = imports["create_genesis_manifest"]
    initialize_genesis = imports["initialize_genesis"]
    Ed25519Signer = imports["Ed25519Signer"]
    CandidatePublisher = imports["CandidatePublisher"]
    store = SQLiteStore(str(db_path))
    try:
        authority_signer = Ed25519Signer()
        worker_signer = Ed25519Signer()
        authority_key = ActorKey(
            "benchmark:authority",
            "authority-key",
            authority_signer.kind,
            authority_signer.signer_id,
            ("asset.publish", "contract.publish", "worker.register"),
        )
        worker_key = ActorKey(
            "benchmark:worker",
            "worker-key",
            worker_signer.kind,
            worker_signer.signer_id,
            ("worker.submit",),
        )
        genesis = create_genesis_manifest(
            "benchmark-planning", (authority_key, worker_key), "policy:benchmark-v1"
        )
        initialize_genesis(store, genesis)
        publisher = CandidatePublisher(
            store, store, genesis, authority_key, authority_signer
        )
        WorkerRegistration = imports["WorkerRegistration"]
        registration = WorkerRegistration(
            worker_id=worker_key.actor_id,
            actor_id=worker_key.actor_id,
            key_id=worker_key.key_id,
            version="1",
        )
        decision = publisher.publish(
            (imports["worker_registration_effect"](registration),),
            idempotency_key="benchmark:worker-registration",
        )
        if not decision.accepted:
            raise RuntimeError("signed Worker registration was rejected")
        host = imports["WorkerHost"](
            imports["MockWorker"](worker_id=worker_key.actor_id),
            genesis,
            worker_key,
            worker_signer,
            registration_version="1",
        )
        return store, publisher, genesis, host, authority_key, authority_signer
    except Exception:
        store.close()
        raise


def _publish_history(count: int, publisher, imports: dict[str, Any]) -> list[Any]:
    assets = []
    effects = []
    contracts = []
    build_asset = imports["build_control_plane_asset"]
    for index in range(count):
        asset = build_asset(
            name=f"history_source_{index}",
            content=f"closed benchmark source {index}",
            origin="human",
            trust_tier="human",
        )
        contract = imports["build_control_plane_contract"](
            name=f"history_task_{index}",
            inputs=(asset.name,),
            outputs=(f"history_result_{index}",),
            activation=asset.name,
            budget=1,
        )
        assets.append(asset)
        contracts.append(contract)
        effects.extend(
            (
                imports["asset_proposal_effect"](asset),
                imports["contract_declaration_effect"](contract),
            )
        )
    if effects:
        decision = publisher.publish(
            tuple(effects), idempotency_key=f"benchmark:closed-history:{count}"
        )
        if not decision.accepted:
            raise RuntimeError("signed background history was rejected")
    return contracts


def _close_history(contracts, host, store, imports: dict[str, Any]) -> None:
    worker = host.worker
    for index, contract in enumerate(contracts):
        worker.set_output(
            contract.name, f'/exec {{"outputs":{{"history_result_{index}":"ok"}}}}'
        )
        claim_candidate = host.claim_candidate(
            request_id=f"benchmark:history-claim:{index}", contract_id=contract.id
        )
        claimed = imports["claim_next_package"](
            store,
            worker_id=host.worker_id,
            contract_id=contract.id,
            expected_registration_version=host.registration_version,
            claim_candidate=claim_candidate,
        )
        if claimed is None:
            raise RuntimeError("signed background task could not be claimed")
        imports["execute_claimed_package"](claimed, host, store)
    terminal_ids = {
        record.payload.get("contract_id")
        for _revision, record in store.scan_runtime_records(
            record_type="lifecycle.terminal"
        )
        if record.payload.get("terminal") == "complete"
    }
    if not {item.id for item in contracts}.issubset(terminal_ids):
        raise RuntimeError("background signed lifecycle did not close every task")


def _root_contract(history: int, size: int, imports: dict[str, Any]):
    description = json.dumps(
        {"allowed_inputs": ["benchmark_source"], "instructions": "benchmark"},
        sort_keys=True,
        separators=(",", ":"),
    )
    return imports["build_control_plane_contract"](
        name="planning_root",
        description=description,
        inputs=("benchmark_source",),
        outputs=("benchmark_result",),
        activation="benchmark_source",
        budget=size + 1,
    )


def _blueprint(index: int, *, include_budget: bool = True) -> str:
    child_description = json.dumps(
        {
            "allowed_inputs": ["benchmark_source"],
            "instructions": "Publish one bounded benchmark task.",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    child: dict[str, Any] = {
        "name": f"planned_{index}",
        "description": child_description,
        "inputs": ["benchmark_source"],
        "outputs": ["benchmark_result"],
        "activation": "benchmark_source",
        "tool_scope": [],
    }
    if include_budget:
        child["budget"] = 1
    return json.dumps({"contracts": [child]}, sort_keys=True, separators=(",", ":"))


def _run_planned(*, mode: str, size: int, publisher, imports: dict[str, Any]):
    """Run a plan case and return accepted declarations plus depth/edge data."""
    compile_blueprint = imports["compile_planning_blueprint"]
    contract_from_dict = imports["contract_from_dict"]
    root = imports["plan_root"]
    remaining = size + 1
    accepted_ids: list[str] = []
    publication_results: list[dict[str, Any]] = []
    max_depth = 0
    depths = {root.id: 0}

    def commit(parent, effects, label: str) -> None:
        decision = publisher.publish(
            tuple(effects), idempotency_key=f"benchmark:plan:{mode}:{label}"
        )
        publication_results.append(
            {"accepted": bool(decision.accepted), "effect_count": len(effects)}
        )
        if not decision.accepted:
            raise PublicationAttemptError(
                f"signed {mode} planning publication was rejected",
                publication_results,
            )

    if mode == "one_batch":
        blueprint = json.dumps(
            {
                "contracts": [
                    {
                        "name": f"planned_{index}",
                        "description": json.dumps(
                            {
                                "allowed_inputs": ["benchmark_source"],
                                "instructions": "Publish one bounded benchmark task.",
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "inputs": ["benchmark_source"],
                        "outputs": ["benchmark_result"],
                        "activation": "benchmark_source",
                        "budget": 1,
                        "tool_scope": [],
                    }
                    for index in range(size)
                ]
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        effects = compile_blueprint(
            root, {"planning_blueprint": blueprint}, allowance=remaining
        )
        commit(root, effects, "batch")
        accepted_ids.extend(effect.payload["contract"]["id"] for effect in effects)
        depth = 1 if effects else 0
        return accepted_ids, {
            "edges": len(effects),
            "max_depth": depth,
            "planning_publications": publication_results,
        }

    current_parent = root
    for index in range(size):
        effects = compile_blueprint(
            current_parent,
            {"planning_blueprint": _blueprint(index)},
            allowance=remaining,
        )
        commit(current_parent, effects, str(index))
        child_data = effects[0].payload["contract"]
        child = contract_from_dict(child_data)
        accepted_ids.append(child.id)
        depth = depths.get(current_parent.id, 0) + 1
        depths[child.id] = depth
        max_depth = max(max_depth, depth)
        remaining -= child.budget
        if mode == "chain":
            current_parent = child
            remaining = child.budget
    return accepted_ids, {
        "edges": size,
        "max_depth": max_depth,
        "planning_publications": publication_results,
    }


def _contract_from_dict(data: dict[str, Any]):
    from aigineering.protocol.wire import contract_from_dict

    return contract_from_dict(data)


def _public_counts(store) -> dict[str, int]:
    records = store.scan_runtime_records()
    contracts = store.get_all_contracts()
    assets = store.get_all_assets()
    terminal = {
        record.payload.get("contract_id")
        for _revision, record in records
        if record.record_type == "lifecycle.terminal"
    }
    contracts_by_id = {contract.id: contract for contract in contracts}
    unclosed = sum(contract_id not in terminal for contract_id in contracts_by_id)
    return {
        "runtime_records": len(records),
        "contracts": len(contracts),
        "assets": len(assets),
        "closed_contracts": len(contracts) - unclosed,
        "unclosed_contracts": unclosed,
    }


def _install_instrumentation(store, *, interval: int = 1000):
    counters = {
        name: {"calls": 0, "returned_rows": 0}
        for name in ("get_all_contracts", "get_all_assets", "scan_runtime_records")
    }
    originals = {}
    for method_name, counter in counters.items():
        original = getattr(store, method_name)
        originals[method_name] = original

        def wrapper(*args, __name=method_name, __original=original, **kwargs):
            result = __original(*args, **kwargs)
            counters[__name]["calls"] += 1
            counters[__name]["returned_rows"] += len(result)
            return result

        setattr(store, method_name, wrapper)
    select_count = 0

    def trace(statement: str):
        nonlocal select_count
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            select_count += 1

    store._conn.set_trace_callback(trace)
    vm_calls = 0

    def progress():
        nonlocal vm_calls
        vm_calls += 1
        return 0

    store._conn.set_progress_handler(progress, interval)
    return counters, originals, trace, interval, lambda: (select_count, vm_calls)


def _reset_instrumentation(store, setup):
    counters, originals, _trace, _interval, values = setup
    store._conn.set_trace_callback(None)
    store._conn.set_progress_handler(None, 0)
    for name, original in originals.items():
        setattr(store, name, original)
    return counters, values()


def _explain(store) -> dict[str, Any]:
    plans: dict[str, Any] = {}
    for label, sql in (
        ("all_runtime_records", "SELECT * FROM runtime_records ORDER BY revision"),
        ("contracts", "SELECT * FROM contracts WHERE parent_id = ? ORDER BY id"),
        ("assets", "SELECT * FROM assets WHERE name = ? ORDER BY id"),
    ):
        rows = store._conn.execute(
            "EXPLAIN QUERY PLAN " + sql, ("sample",) if "?" in sql else ()
        ).fetchall()
        plans[label] = [{"operation": str(row[3])[:240]} for row in rows]
    return plans


def _profile_summary(profile: cProfile.Profile) -> list[dict[str, Any]]:
    stats = pstats.Stats(profile)
    entries = []
    for (filename, line, function), values in sorted(
        stats.stats.items(), key=lambda item: item[1][3], reverse=True
    )[:25]:
        primitive_calls, total_calls, total_time, cumulative_time, _callers = values
        entries.append(
            {
                "function": function,
                "module": Path(filename).name[:100],
                "line": int(line),
                "primitive_calls": int(primitive_calls),
                "calls": int(total_calls),
                "total_seconds": float(total_time),
                "cumulative_seconds": float(cumulative_time),
            }
        )
    return entries


def _sample(
    *,
    history: int,
    size: int,
    mode: str,
    instrumented: bool,
    sample_index: int,
    imports: dict[str, Any],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="aig-planning-case-") as directory:
        domain_started = time.perf_counter_ns()
        store, publisher, _genesis, host, _key, _signer = _new_domain(
            Path(directory) / "runtime.sqlite", imports
        )
        domain_setup_ms = (time.perf_counter_ns() - domain_started) / 1_000_000
        try:
            build_started = time.perf_counter_ns()
            background = _publish_history(history, publisher, imports)
            _close_history(background, host, store, imports)
            root = _root_contract(history, size, imports)
            source = imports["build_control_plane_asset"](
                name="benchmark_source",
                content="fixed benchmark planning input",
                origin="human",
                trust_tier="human",
            )
            root_decision = publisher.publish(
                (
                    imports["asset_proposal_effect"](source),
                    imports["contract_declaration_effect"](root),
                ),
                idempotency_key=f"benchmark:planning-root:{mode}:{size}",
            )
            if not root_decision.accepted:
                raise RuntimeError("signed planning parent declaration was rejected")
            imports["plan_root"] = root
            build_ms = (time.perf_counter_ns() - build_started) / 1_000_000
            closed_count = len(
                store.scan_runtime_records(record_type="lifecycle.terminal")
            )
            if closed_count != history:
                raise RuntimeError("closed-history terminal count did not match H")

            instrument_setup = _install_instrumentation(store) if instrumented else None
            profile = cProfile.Profile() if instrumented else None
            if profile is not None:
                profile.enable()
            if instrumented:
                tracemalloc.start()
            started = time.perf_counter_ns()
            accepted_ids, topology = _run_planned(
                mode=mode, size=size, publisher=publisher, imports=imports
            )
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            peak_bytes = None
            if instrumented:
                _current, peak_bytes = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            if profile is not None:
                profile.disable()
            counters = None
            sqlite_counts = None
            explain = None
            if instrumented and instrument_setup is not None:
                counters, (select_count, vm_calls) = _reset_instrumentation(
                    store, instrument_setup
                )
                interval = instrument_setup[3]
                explain = _explain(store)
                sqlite_counts = {
                    "select_statements": select_count,
                    "vm_progress_callbacks": vm_calls,
                    "progress_interval_opcodes": interval,
                    "vm_opcode_proxy_product": vm_calls * interval,
                }
            counts = _public_counts(store)
            if len(accepted_ids) != size or counts["contracts"] != history + size + 1:
                # +1 benchmark root is a declared parent for all planning modes.
                raise RuntimeError(
                    "planned publication counts did not match requested P"
                )
            terminals = store.scan_runtime_records(record_type="lifecycle.terminal")
            terminal_contracts = {
                record.payload.get("contract_id") for _revision, record in terminals
            }
            if len(terminal_contracts) != history:
                raise RuntimeError("planning workload changed the closed background")
            result: dict[str, Any] = {
                "sample": sample_index,
                "status": "ok",
                "domain_setup_ms": domain_setup_ms,
                "history_build_ms": build_ms,
                "planning_elapsed_ms": elapsed_ms,
                "facts_and_materializations": counts,
                "complexity_dimensions": {
                    "P_new_child_contracts": size,
                    "H_closed_history_contracts": history,
                    "A_unclosed_relevant_contracts": counts["unclosed_contracts"],
                    "claimability": "not measured; no claim was issued",
                },
                "topology": {
                    **topology,
                    "accepted_child_declarations": size,
                    "planning_candidate_publication_count": len(
                        topology["planning_publications"]
                    ),
                    "unclosed_planned_contracts": size,
                    "active_contracts_including_benchmark_parent": counts[
                        "unclosed_contracts"
                    ],
                },
            }
            if instrumented:
                result["instrumented_elapsed_ms"] = elapsed_ms
                result["instrumentation"] = {
                    "store_methods": counters,
                    "sqlite": sqlite_counts,
                    "explain_query_plan": explain,
                    "tracemalloc_peak_bytes": int(peak_bytes or 0),
                    "python_profile_top": _profile_summary(profile),
                }
            return result
        finally:
            store.close()


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * p
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def _summarize(samples: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [float(sample[key]) for sample in samples]
    return {
        "sample_count": len(values),
        "samples_ms": values,
        "median_ms": float(statistics.median(values)),
        "p95_ms": _percentile(values, 0.95),
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    imports = _imports(installed=(args.installed or args.source_archive))
    metadata = _metadata(args, imports["__version__"])
    cells: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for history in args.histories:
        for size in args.sizes:
            for mode in args.modes:
                cell: dict[str, Any] = {
                    "P": size,
                    "H": history,
                    "A": size + 1,
                    "A_measurement": "unclosed_relevant_contracts_including_parent",
                    "mode": mode,
                    "timing": [],
                    "instrumentation": [],
                    "status": "ok",
                }
                for sample_index in range(args.samples):
                    for instrumented in (False, True):
                        pass_name = "instrumentation" if instrumented else "timing"
                        try:
                            sample = _sample(
                                history=history,
                                size=size,
                                mode=mode,
                                instrumented=instrumented,
                                sample_index=sample_index,
                                imports=imports,
                            )
                            cell[pass_name].append(sample)
                        except Exception as exc:
                            failure = {
                                "P": size,
                                "H": history,
                                "mode": mode,
                                "sample": sample_index,
                                "pass": pass_name,
                                "error_type": type(exc).__name__,
                                "error": "operation failed; details are in private stderr",
                                "publication_attempts": list(
                                    getattr(exc, "publication_attempts", ())
                                ),
                            }
                            print(
                                f"benchmark case failed: {type(exc).__name__}",
                                file=sys.stderr,
                            )
                            traceback.print_exc(file=sys.stderr)
                            failures.append(failure)
                            cell["status"] = "failed"
                            cell.setdefault("failures", []).append(failure)
                            break
                    if cell["status"] != "ok":
                        break
                if cell["timing"]:
                    cell["timing_summary"] = _summarize(
                        cell["timing"], "planning_elapsed_ms"
                    )
                if cell["instrumentation"]:
                    cell["instrumentation_summary"] = _summarize(
                        cell["instrumentation"], "instrumented_elapsed_ms"
                    )
                cells.append(cell)
                if cell["status"] != "ok":
                    report = {
                        **metadata,
                        "exit_code": 1,
                        "status": "failed",
                        "failure_classification": "benchmark_case_failed",
                        "completed_cells": cells,
                        "failures": failures,
                    }
                    return 1, report
    return 0, {
        **metadata,
        "exit_code": 0,
        "status": "complete",
        "failure_classification": None,
        "cells": cells,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _validate(parser, args)
    try:
        code, report = run(args)
    except Exception as exc:
        print(f"benchmark setup failed: {type(exc).__name__}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        report = {
            "benchmark": "aigineering-planning-query-growth",
            "schema_version": 1,
            "driver_checkout_revision": _git_revision(),
            "imported_source_revision": args.imported_source_revision,
            "exit_code": 1,
            "status": "failed",
            "failure_classification": "setup_failed",
            "failures": [
                {
                    "error_type": type(exc).__name__,
                    "error": "setup failed; details are in private stderr",
                }
            ],
        }
        code = 1
    try:
        _write_report(args.output, report)
    except FileExistsError:
        parser.error("refusing to overwrite existing output")
    print(json.dumps({"status": report["status"], "exit_code": code}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
