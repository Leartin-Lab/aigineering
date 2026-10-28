"""Smoke tests for signed planning-query growth measurements."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_planning_queries.py"


def test_planning_benchmark_smoke_covers_signed_modes_and_closed_history(tmp_path):
    output = tmp_path / "planning-benchmark.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--smoke", "--output", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["benchmark"] == "aigineering-planning-query-growth"
    assert report["schema_version"] == 1
    assert report["status"] == "complete"
    assert report["source_sha256"]["benchmark_script"]
    assert report["source_sha256"]["package_python_file_count"] > 0
    assert "core/sqlite_schema.py" in report["source_sha256"]["package_python_files"]
    assert len(report["cells"]) == 6

    for cell in report["cells"]:
        assert cell["status"] == "ok"
        assert cell["P"] == 2
        assert cell["H"] in {0, 2}
        assert cell["A"] == 3
        assert cell["mode"] in {"one_batch", "siblings_sequential", "chain"}
        assert len(cell["timing"]) == len(cell["instrumentation"]) == 1
        assert cell["timing_summary"]["sample_count"] == 1
        assert cell["instrumentation_summary"]["sample_count"] == 1
        for sample in (*cell["timing"], *cell["instrumentation"]):
            assert sample["status"] == "ok"
            assert sample["facts_and_materializations"]["closed_contracts"] == cell["H"]
            assert sample["facts_and_materializations"]["contracts"] == cell["H"] + 3
            assert sample["complexity_dimensions"]["P_new_child_contracts"] == 2
            assert (
                sample["complexity_dimensions"]["H_closed_history_contracts"]
                == cell["H"]
            )
            assert sample["complexity_dimensions"]["A_unclosed_relevant_contracts"] == 3
            publications = sample["topology"]["planning_publications"]
            assert len(publications) == (1 if cell["mode"] == "one_batch" else 2)
            assert all(item["accepted"] for item in publications)
            assert sum(item["effect_count"] for item in publications) == 2
            assert sample["topology"]["planning_candidate_publication_count"] == len(
                publications
            )
            assert sample["topology"]["edges"] == 2
        measured = cell["instrumentation"][0]["instrumentation"]
        assert measured["sqlite"]["select_statements"] > 0
        assert measured["sqlite"]["progress_interval_opcodes"] == 1000
        assert "estimated_vm_opcode_interval" not in measured["sqlite"]
        assert measured["sqlite"]["vm_opcode_proxy_product"] >= 0
        assert measured["store_methods"]["scan_runtime_records"]["calls"] > 0
        assert measured["explain_query_plan"]["all_runtime_records"]


def test_planning_benchmark_refuses_to_overwrite_an_existing_report(tmp_path):
    output = tmp_path / "existing.json"
    original = "preserve this file\n"
    output.write_text(original, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--smoke", "--output", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert output.read_text(encoding="utf-8") == original
