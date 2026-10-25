from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path


_SCRIPT_HELPERS = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "benchmark_asset_reducer.py")
)


def test_asset_reducer_benchmark_smoke_is_signed_and_pairs_twins(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "smoke.json"
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "benchmark_asset_reducer.py"),
            "--smoke",
            "--package-root",
            str(root / "src"),
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["benchmark"] == "aigineering-asset-reducer-locality"
    assert report["execution"]["accepted_publications"] > 0
    assert report["execution"]["signed_history_closed"] is True
    assert report["execution"]["twin_semantics_equal"] is True
    assert all(cell["twin_decisions_equal"] for cell in report["measurement"]["cells"])
    assert all(
        cell["read_only_rebuild_digest_equal"]
        for cell in report["measurement"]["cells"]
    )
    assert report["execution"]["case_count"] == 3
    microcases = report["execution"]["correctness_microcases"]
    assert microcases["asset_before_contract"]["accepted"]
    assert microcases["contract_before_asset"]["activation_trace"]
    assert microcases["activation_conjunction_missing_one"]["left_did_not_activate"]
    assert microcases["activation_conjunction_missing_one"]["both_names_activated"]
    assert microcases["same_batch_asset_before_declaration"]["activation_trace"]
    assert (
        microcases["same_batch_asset_before_declaration"]["contract_terminal"]
        == "complete"
    )
    assert (
        microcases["same_batch_declaration_before_satisfying_asset"][
            "contract_terminal"
        ]
        == "complete"
    )
    assert microcases["duplicate_same_name_same_content"]["effect_count"] == 2
    assert microcases["duplicate_same_name_same_content"]["unique_asset_ids"] == 1
    assert (
        microcases["duplicate_same_name_sequential_publications"]["publication_count"]
        == 2
    )
    assert (
        microcases["duplicate_same_name_sequential_publications"][
            "decision_asset_count"
        ]
        == 2
    )
    assert (
        microcases["duplicate_same_name_sequential_publications"]["unique_asset_ids"]
        == 2
    )
    assert microcases["signed_intermediate_cancellation_after_descendant_declaration"][
        "intermediate_signed_cancelled"
    ]
    cancellation = microcases[
        "signed_intermediate_cancellation_after_descendant_declaration"
    ]
    assert cancellation["parent_completed_after_output"]
    assert cancellation["cancelled_intermediate_preserved"]
    assert cancellation["unfinished_grandchild_terminal"] == "cancelled"
    assert cancellation["twins"]["read_only_rebuild_digest_equal"]
    cascade = microcases["descendant_and_independent_acceptance"]
    assert cascade["root_completed_after_attestation"]
    assert cascade["claimed_verifier_cancelled_after_acceptance"]
    assert cascade["attestation_twin"]["read_only_rebuild_digest_equal"]
    assert all(
        cell["actual_unclosed_contracts"] >= 2
        for cell in report["measurement"]["cells"]
    )
    assert all(
        cell["unique_asset_ids"] == cell["P"] for cell in report["measurement"]["cells"]
    )
    assert all(
        cell["accepted_effects"] == cell["P"]
        and all(attempt["accepted"] for attempt in cell["publication_attempts"])
        for cell in report["measurement"]["cells"]
    )
    assert all(
        cell["accepted_publications"]
        == (1 if cell["mode"] == "asset_batch" else cell["P"])
        for cell in report["measurement"]["cells"]
    )
    assert all(
        cell["activation_fanout"] >= 2 for cell in report["measurement"]["cells"]
    )
    for cell in report["measurement"]["cells"]:
        assert cell["history_identity_counts"]["assets_H"] == 2 * cell["H"]
        assert cell["history"]["source_assets"] == cell["H"]
        assert cell["history"]["output_assets"] == cell["H"]
        assert cell["history"]["assets"] == 2 * cell["H"]
        assert cell["actual_unclosed_contracts"] == cell["A"]
        assert cell["fixed_sparse_contract_count"] == 2
        assert cell["fixed_sparse_contracts_unterminal"] is True
        assert cell["descendant_count"] == sum(
            reference["parented"] for reference in cell["contract_reference_counts"]
        )
        assert cell["descendant_depth"] >= 0
        assert all(
            reference["activation_refs"] >= 0
            and reference["input_refs"] >= 0
            and reference["output_refs"] >= 0
            for reference in cell["contract_reference_counts"]
        )
        classification = cell["row_classification_by_stage"]
        historical = cell["unrelated_history_rows_by_stage"]
        for stage, classes in classification["sqlite_materialized_rows"].items():
            assert historical["sqlite_materialized_rows"][stage] == classes.get(
                "known_history_H", 0
            )
            assert sum(classes.values()) == cell["instrumentation"][
                "stage_sql_rows_fetched"
            ].get(stage, 0)
        for stage, methods in classification["store_returned_objects"].items():
            for method, classes in methods.items():
                assert historical["store_returned_objects"][stage][method] == (
                    classes.get("known_history_H", 0)
                )
                assert sum(classes.values()) == cell["instrumentation"][
                    "stage_returned_rows"
                ].get(stage, {}).get(method, 0)
        if cell["H"]:
            for stage in (
                "full_projection_context",
                "pre_commit_reduce_asset_facts",
                "writer_reduce_asset_facts",
            ):
                assert historical["sqlite_materialized_rows"][stage] > 0
                assert any(
                    amount > 0
                    for amount in historical["store_returned_objects"][stage].values()
                )
    assert report["measurement"]["context_loader_calls"] > 0
    assert report["measurement"]["pre_commit_reducer_calls"] > 0
    assert report["measurement"]["writer_reducer_calls"] > 0
    assert (
        report["measurement"]["cells"][0]["instrumentation"][
            "full_projection_context_calls"
        ]
        == 1
    )
    first = report["measurement"]["cells"][0]["instrumentation"]
    assert first["full_projection_context_rows"] == first["context_rows"]
    assert first["writer_ingress_consequences_calls"] == first["writer_reducer_calls"]
    assert first["decoded_rows"]["runtime_records"] > 0
    assert first["sqlite_rows_fetched"] == sum(first["stage_sql_rows_fetched"].values())
    assert first["sqlite_rows_fetched"] > sum(first["decoded_rows"].values())
    for name, total in first["store_returned_rows"].items():
        assert (
            sum(stage.get(name, 0) for stage in first["stage_returned_rows"].values())
            == total
        )
    for name, total in first["decoded_rows"].items():
        assert (
            sum(stage.get(name, 0) for stage in first["stage_decoded_rows"].values())
            == total
        )
    assert first["sqlite_vm_work_proxy"] == (
        first["sqlite_vm_progress_callbacks"] * first["sqlite_vm_interval"]
    )
    assert "duration_stages" in report["measurement"]["counter_relationship_notes"]
    assert report["measurement"]["not_measured"]
    assert report["measurement"]["counter_relationship_notes"]["vm_work"].startswith(
        "callbacks multiplied"
    )
    assert report["source"]["package_python_file_count"] >= 200
    assert report["source"]["imported_source_revision"] is None
    assert report["source"]["requested_source_revision"] is None
    assert (
        len(report["source"]["package_python_files"])
        == report["source"]["package_python_file_count"]
    )
    assert "/private/tmp" not in output.read_text()


def test_benchmark_refuses_to_overwrite_report(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "already.json"
    output.write_text("keep")
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "benchmark_asset_reducer.py"),
            "--smoke",
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert output.read_text() == "keep"


def test_package_root_failure_keeps_parseable_failure_report(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "failure.json"
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "benchmark_asset_reducer.py"),
            "--smoke",
            "--package-root",
            str(tmp_path / "missing-package-root"),
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    report = json.loads(output.read_text())
    assert report["failure_type"] == "RuntimeError"
    assert report["failure_phase"] == "package_import"
    assert report["completed_cells"] == 0
    assert report["source"]["imported_source_revision"] is None
    assert "/private/tmp" not in output.read_text()


def test_expected_package_manifest_mismatch_is_reported(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "manifest-mismatch.json"
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "benchmark_asset_reducer.py"),
            "--smoke",
            "--package-root",
            str(root / "src"),
            "--expected-package-manifest-sha256",
            "0" * 64,
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    report = json.loads(output.read_text())
    assert report["failure_phase"] == "package_manifest"
    assert report["source"]["package_python_file_count"] >= 200
    assert report["source"]["package_manifest_sha256"] != "0" * 64
    assert report["completed_cells"] == 0
    assert "/private/tmp" not in output.read_text()


def test_p95_uses_nearest_rank_for_small_samples() -> None:
    p95 = _SCRIPT_HELPERS["_nearest_rank_p95"]
    assert p95([1.0, 2.0, 3.0]) == 3.0
    assert p95([9.0]) == 9.0
