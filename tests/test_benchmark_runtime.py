"""Smoke tests for the standalone runtime benchmark."""

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_runtime.py"


def test_benchmark_runtime_subprocess_smoke(tmp_path):
    output = tmp_path / "benchmark.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--sizes",
            "2",
            "--samples",
            "2",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["benchmark"] == "aigineering-runtime"
    assert report["schema_version"] == 1
    assert report["sizes"][0]["history_size"] == 2
    size_report = report["sizes"][0]
    assert size_report["actual_record_counts"]["runtime_records"] > 0
    assert size_report["actual_record_counts"]["assets"] == 3
    assert size_report["actual_record_counts"]["contracts"] == 3

    for operation in ("commit", "audit", "rebuild"):
        metrics = size_report["operations"][operation]
        assert metrics["sample_count"] == 2
        assert len(metrics["samples_ms"]) == 2
        assert len(metrics["peak_memory_bytes_by_sample"]) == 2
        assert all(isinstance(value, (int, float)) for value in metrics["samples_ms"])
        assert all(value >= 0 for value in metrics["samples_ms"])
        assert isinstance(metrics["median_ms"], (int, float))
        assert isinstance(metrics["p95_ms"], (int, float))
        assert metrics["median_ms"] >= 0
        assert metrics["p95_ms"] >= 0
        assert all(value >= 0 for value in metrics["peak_memory_bytes_by_sample"])
