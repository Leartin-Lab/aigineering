#!/usr/bin/env python3
"""Measure signed runtime commitments and rebuildable read projections.

The benchmark creates an isolated temporary SQLite domain for every sample.
History is admitted through signed Candidates so the measured operations use
the same public boundary as ordinary runtime traffic.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from pathlib import Path
from typing import Any, Callable


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPO_ROOT / "src"
if _SOURCE_ROOT.is_dir() and str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from aigineering import __version__  # noqa: E402
from aigineering.core.candidate_publisher import CandidatePublisher  # noqa: E402
from aigineering.core.control_plane import (  # noqa: E402
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.domain import initialize_genesis  # noqa: E402
from aigineering.core.signing import Ed25519Signer  # noqa: E402
from aigineering.core.sqlite_store import SQLiteStore  # noqa: E402
from aigineering.core.task_productivity import project_task_productivity  # noqa: E402
from aigineering.protocol.candidate import (  # noqa: E402
    ActorKey,
    GenesisManifest,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (  # noqa: E402
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Contract  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark signed Aigineering runtime operations."
    )
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[10, 100],
        metavar="N",
        help="positive history sizes to benchmark (default: 10 100)",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="JSON report path; an existing file is never overwritten",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        metavar="N",
        help="positive samples per history size (default: 3)",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.sizes or any(size <= 0 for size in args.sizes):
        parser.error("--sizes values must be positive integers")
    if args.samples <= 0:
        parser.error("--samples must be a positive integer")
    if args.output.exists():
        parser.error(f"refusing to overwrite existing output: {args.output}")


def _git_metadata() -> dict[str, Any]:
    """Return public revision state without reading credentials or config."""

    def run(*command: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", *command],
                cwd=_REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    revision_result = run("rev-parse", "HEAD")
    status_result = run("status", "--porcelain")
    revision = (
        revision_result.stdout.strip()
        if revision_result is not None and revision_result.returncode == 0
        else None
    )
    dirty = (
        bool(status_result.stdout.strip())
        if status_result is not None and status_result.returncode == 0
        else None
    )
    return {"revision": revision, "dirty": dirty}


def _environment_metadata(argv: list[str]) -> dict[str, Any]:
    return {
        "argv": argv,
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "sqlite": {"version": sqlite3.sqlite_version},
        "platform": platform.platform(),
        "package": {"name": "aigineering", "version": __version__},
        "git": _git_metadata(),
    }


def _publish_work_item(
    publisher: CandidatePublisher, *, index: int, idempotency_key: str
) -> Contract:
    asset = build_control_plane_asset(
        name=f"benchmark_asset_{index}",
        content=f"benchmark content {index}",
        origin="human",
        trust_tier="human",
    )
    contract = build_control_plane_contract(
        name=f"benchmark_contract_{index}",
        inputs=(asset.name,),
        outputs=(f"benchmark_output_{index}",),
        activation=asset.name,
        budget=1,
    )
    decision = publisher.publish(
        (asset_proposal_effect(asset), contract_declaration_effect(contract)),
        idempotency_key=idempotency_key,
    )
    if not decision.accepted:
        raise RuntimeError("benchmark Candidate was rejected")
    return contract


def _new_domain(
    db_path: Path,
) -> tuple[SQLiteStore, CandidatePublisher, GenesisManifest]:
    store = SQLiteStore(str(db_path))
    try:
        signer = Ed25519Signer()
        actor_key = ActorKey(
            "benchmark:root",
            "benchmark-root-1",
            signer.kind,
            signer.signer_id,
            ("asset.publish", "contract.publish"),
        )
        genesis = create_genesis_manifest(
            "benchmark-runtime", (actor_key,), "policy:benchmark-v1"
        )
        initialize_genesis(store, genesis)
        return (
            store,
            CandidatePublisher(store, store, genesis, actor_key, signer),
            genesis,
        )
    except Exception:
        store.close()
        raise


def _record_counts(store: SQLiteStore) -> dict[str, Any]:
    records = store.scan_runtime_records()
    by_type = Counter(record.record_type for _, record in records)
    return {
        "runtime_records": len(records),
        "runtime_records_by_type": dict(sorted(by_type.items())),
        "assets": len(store.get_all_assets()),
        "contracts": len(store.get_all_contracts()),
        "trace_events": len(store.get_all()),
    }


def _measure(operation: Callable[[], Any]) -> tuple[dict[str, Any], Any]:
    tracemalloc.start()
    started = time.perf_counter_ns()
    try:
        result = operation()
    finally:
        elapsed_ns = time.perf_counter_ns() - started
        _current, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return {
        "elapsed_ms": elapsed_ns / 1_000_000,
        "peak_memory_bytes": peak_bytes,
    }, result


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def _summarize(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = [float(item["elapsed_ms"]) for item in measurements]
    peaks = [int(item["peak_memory_bytes"]) for item in measurements]
    return {
        "sample_count": len(elapsed),
        "samples_ms": elapsed,
        "median_ms": float(statistics.median(elapsed)),
        "p95_ms": _percentile(elapsed, 0.95),
        "peak_memory_bytes": max(peaks),
        "peak_memory_bytes_by_sample": peaks,
    }


def _run_sample(history_size: int, sample_number: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="aig_benchmark_") as directory:
        store, publisher, _genesis = _new_domain(Path(directory) / "runtime.db")
        try:
            root_contract: Contract | None = None
            for index in range(history_size):
                root_contract = _publish_work_item(
                    publisher,
                    index=index,
                    idempotency_key=f"benchmark-history-{index}",
                )
            if root_contract is None:
                raise RuntimeError("benchmark history unexpectedly empty")

            before_counts = _record_counts(store)
            commit_measurement, _committed_contract = _measure(
                lambda: _publish_work_item(
                    publisher,
                    index=history_size,
                    idempotency_key=f"benchmark-sample-{sample_number}",
                )
            )
            expected_digest = store.runtime_materialization_digest()
            audit_measurement, audit = _measure(
                lambda: project_task_productivity(root_contract, store)
            )
            rebuild_measurement, rebuild_digest = _measure(
                lambda: store.rebuild_runtime_materializations()
            )
            post_rebuild_digest = store.runtime_materialization_digest()
            if (
                rebuild_digest != expected_digest
                or post_rebuild_digest != expected_digest
            ):
                raise RuntimeError(
                    "runtime materialization rebuild changed durable views"
                )
            after_counts = _record_counts(store)
            return {
                "sample": sample_number,
                "before_record_counts": before_counts,
                "after_record_counts": after_counts,
                "commit": commit_measurement,
                "audit": audit_measurement,
                "rebuild": rebuild_measurement,
                "audit_contract_count": audit["contract_count"],
                "rebuild_digest_matches": True,
            }
        finally:
            store.close()


def _run_size(history_size: int, sample_count: int) -> dict[str, Any]:
    samples = [_run_sample(history_size, number) for number in range(sample_count)]
    first = samples[0]
    return {
        "history_size": history_size,
        "actual_record_counts": first["after_record_counts"],
        "samples": samples,
        "operations": {
            operation: _summarize([sample[operation] for sample in samples])
            for operation in ("commit", "audit", "rebuild")
        },
    }


def run_benchmark(sizes: list[int], samples: int, output: Path) -> dict[str, Any]:
    report = {
        "benchmark": "aigineering-runtime",
        "schema_version": 1,
        **_environment_metadata(list(sys.argv)),
        "sizes": [_run_size(size, samples) for size in sizes],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    run_benchmark(args.sizes, args.samples, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
