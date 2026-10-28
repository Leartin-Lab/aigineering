from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from collections import Counter
from pathlib import Path


_SCRIPT_HELPERS = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "benchmark_asset_reducer.py")
)


def _package_root(root: Path) -> Path:
    return Path(
        os.environ.get("AIGINEERING_D0C_PACKAGE_ROOT", str(root / "src"))
    ).resolve()


def _assert_counter_relationships(instrumentation: dict) -> None:
    sql_rows = instrumentation["stage_sql_rows_fetched"]
    sql_classes = instrumentation["stage_sql_row_classes"]
    assert set(sql_rows) == set(sql_classes)
    for stage, rows in sql_rows.items():
        assert sum(sql_classes[stage].values()) == rows
    assert sum(sql_rows.values()) == instrumentation["sqlite_rows_fetched"]
    assert (
        sum(instrumentation["sqlite_row_classes"].values())
        == instrumentation["sqlite_rows_fetched"]
    )

    returned_rows = instrumentation["stage_returned_rows"]
    returned_classes = instrumentation["stage_store_row_classes"]
    assert set(returned_rows) == set(returned_classes)
    totals_by_method: Counter[str] = Counter()
    for stage, methods in returned_rows.items():
        assert set(methods) == set(returned_classes[stage])
        for method, rows in methods.items():
            assert sum(returned_classes[stage][method].values()) == rows
            totals_by_method[method] += rows
    assert dict(totals_by_method) == instrumentation["store_returned_rows"]

    for field, total in instrumentation["decoded_rows"].items():
        assert (
            sum(
                counters.get(field, 0)
                for counters in instrumentation["stage_decoded_rows"].values()
            )
            == total
        )
    assert instrumentation["sqlite_vm_work_proxy"] == (
        instrumentation["sqlite_vm_progress_callbacks"]
        * instrumentation["sqlite_vm_interval"]
    )


def _assert_context_entry_path(report: dict, instrumentation: dict) -> bool:
    capabilities = report["source"]["context_loader_api_capabilities"]
    has_candidate_facade = capabilities[
        "projection_context.load_candidate_projection_context"
    ]
    assert (
        capabilities["projection_context.load_asset_proposal_projection_context"]
        is has_candidate_facade
    )
    assert capabilities[
        "projection_context.load_contract_declaration_projection_context"
    ]
    assert capabilities["commitment.load_contract_declaration_projection_context"] is (
        not has_candidate_facade
    )
    active_path = "candidate_facade" if has_candidate_facade else "contract_declaration"
    assert report["source"]["context_loader_entry_path"] == active_path
    assert instrumentation["context_loader_entry_path"] == active_path
    expected_calls = {"candidate_facade": 0, "contract_declaration": 0}
    expected_calls[active_path] = instrumentation["context_loader_calls"]
    assert instrumentation["context_loader_path_calls"] == expected_calls
    return has_candidate_facade


def test_asset_reducer_benchmark_smoke_is_signed_and_pairs_twins(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "smoke.json"
    command = [
        sys.executable,
        str(root / "scripts" / "benchmark_asset_reducer.py"),
        "--smoke",
        "--package-root",
        str(_package_root(root)),
        "--source-revision",
        os.environ.get("AIGINEERING_D0C_SOURCE_LABEL", "test-package-root"),
        "--output",
        str(output),
    ]
    expected_manifest = os.environ.get("AIGINEERING_D0C_EXPECTED_MANIFEST_SHA256")
    if expected_manifest:
        command.extend(["--expected-package-manifest-sha256", expected_manifest])
    result = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["benchmark"] == "aigineering-asset-reducer-locality"
    expected_manifest = os.environ.get("AIGINEERING_D0C_EXPECTED_MANIFEST_SHA256")
    if expected_manifest:
        assert report["source"]["package_manifest_sha256"] == expected_manifest
    assert report["execution"]["accepted_publications"] > 0
    assert report["execution"]["signed_history_closed"] is True
    assert report["execution"]["twin_semantics_equal"] is True
    assert all(cell["twin_decisions_equal"] for cell in report["measurement"]["cells"])
    assert all(
        cell["read_only_rebuild_digest_equal"]
        for cell in report["measurement"]["cells"]
    )
    assert report["execution"]["case_count"] == 3
    assert report["execution"]["expected_timing_and_instrumentation_pairs"] == 3
    microcases = report["execution"]["correctness_microcases"]
    assert len(microcases) == 9
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
    has_candidate_facade = report["source"]["context_loader_api_capabilities"][
        "projection_context.load_candidate_projection_context"
    ]
    assert report["source"]["context_loader_entry_path"] == (
        "candidate_facade" if has_candidate_facade else "contract_declaration"
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
        instrumentation = cell["instrumentation"]
        _assert_context_entry_path(report, instrumentation)
        _assert_counter_relationships(instrumentation)
        assert instrumentation["sqlite_rows_fetched"] == sum(
            instrumentation["stage_sql_rows_fetched"].values()
        )
        assert instrumentation["stage_duration_ms"]["projection_context_total"] >= (
            instrumentation["stage_duration_ms"].get("full_projection_context", 0.0)
        )
        assert all(
            instrumentation["full_projection_context_rows"][field]
            <= instrumentation["context_rows"][field]
            for field in instrumentation["context_rows"]
        )
        if cell["H"]:
            expected_history_stages = [
                "pre_commit_reduce_asset_facts",
                "writer_reduce_asset_facts",
            ]
            if not has_candidate_facade or cell["mixed"]:
                expected_history_stages.append("full_projection_context")
            for stage in expected_history_stages:
                assert historical["sqlite_materialized_rows"][stage] > 0
                assert any(
                    amount > 0
                    for amount in historical["store_returned_objects"][stage].values()
                )
        if (
            cell["case"] in {"asset_batch", "asset_sequential"}
            and has_candidate_facade
            and not cell["mixed"]
        ):
            assert instrumentation["context_rows"] == {
                "contracts": 0,
                "assets": 0,
                "runtime_records": 0,
                "registrations": 0,
                "claim_history_contract_ids": 0,
            }
            assert instrumentation["full_projection_context_calls"] == 0
        elif cell["mixed"]:
            assert instrumentation["full_projection_context_calls"] == 1
            assert (
                instrumentation["full_projection_context_rows"]
                == instrumentation["context_rows"]
            )
    assert report["measurement"]["context_loader_calls"] > 0
    assert report["measurement"]["pre_commit_reducer_calls"] > 0
    assert report["measurement"]["writer_reducer_calls"] > 0
    first = report["measurement"]["cells"][0]["instrumentation"]
    if has_candidate_facade:
        assert first["full_projection_context_calls"] == 0
    else:
        assert first["full_projection_context_calls"] == 1
        assert first["full_projection_context_rows"] == first["context_rows"]
    assert first["writer_ingress_consequences_calls"] == first["writer_reducer_calls"]
    assert first["decoded_rows"]["runtime_records"] > 0
    assert first["sqlite_rows_fetched"] == sum(first["stage_sql_rows_fetched"].values())
    assert first["sqlite_rows_fetched"] > sum(first["decoded_rows"].values())
    _assert_counter_relationships(first)
    assert "duration_stages" in report["measurement"]["counter_relationship_notes"]
    assert report["measurement"]["not_measured"]
    assert report["measurement"]["counter_relationship_notes"]["vm_work"].startswith(
        "callbacks multiplied"
    )
    assert report["source"]["package_python_file_count"] >= 200
    assert report["source"]["imported_source_revision"] is None
    assert report["source"]["requested_source_revision"] == os.environ.get(
        "AIGINEERING_D0C_SOURCE_LABEL", "test-package-root"
    )
    assert (
        len(report["source"]["package_python_files"])
        == report["source"]["package_python_file_count"]
    )
    assert "/private/tmp" not in output.read_text()


def test_benchmark_context_compat_has_six_pairs_and_h16_replay_probe(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "context-compat.json"
    command = [
        sys.executable,
        str(root / "scripts" / "benchmark_asset_reducer.py"),
        "--context-compat",
        "--package-root",
        str(_package_root(root)),
        "--source-revision",
        os.environ.get("AIGINEERING_D0C_SOURCE_LABEL", "test-package-root"),
        "--output",
        str(output),
    ]
    expected_manifest = os.environ.get("AIGINEERING_D0C_EXPECTED_MANIFEST_SHA256")
    if expected_manifest:
        command.extend(["--expected-package-manifest-sha256", expected_manifest])
    result = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["source"]["requested_source_revision"] == os.environ.get(
        "AIGINEERING_D0C_SOURCE_LABEL", "test-package-root"
    )
    if expected_manifest:
        assert report["source"]["package_manifest_sha256"] == expected_manifest
    assert report["execution"]["source_mode"] == "context_compat"
    assert report["execution"]["case_count"] == 6
    assert report["execution"]["expected_timing_and_instrumentation_pairs"] == 6
    assert report["execution"]["timing_execution_count"] == 6
    assert report["execution"]["instrumentation_execution_count"] == 6
    parameters = report["measurement"]["parameters"]
    assert parameters["mode"] == "context_compat"
    assert parameters["P"] == [1, 64]
    assert parameters["H"] == [0, 16, 64]
    assert parameters["modes"] == ["asset_only", "mixed_asset_declare"]
    assert parameters["timing_samples_per_P_H_mode"] == 1
    assert parameters["context_pairs"] == 6
    assert parameters["separate_replay_probe"] == {
        "P": 1,
        "H": 16,
        "timing_pair": False,
    }
    assert [
        (cell["P"], cell["H"], cell["case"]) for cell in report["measurement"]["cells"]
    ] == [
        (64, 0, "asset_only"),
        (64, 16, "asset_only"),
        (64, 64, "asset_only"),
        (1, 0, "mixed_asset_declare"),
        (1, 16, "mixed_asset_declare"),
        (1, 64, "mixed_asset_declare"),
    ]
    has_candidate_facade = report["source"]["context_loader_api_capabilities"][
        "projection_context.load_candidate_projection_context"
    ]
    assert report["source"]["context_loader_entry_path"] == (
        "candidate_facade" if has_candidate_facade else "contract_declaration"
    )
    for cell in report["measurement"]["cells"]:
        instrumentation = cell["instrumentation"]
        _assert_context_entry_path(report, instrumentation)
        _assert_counter_relationships(instrumentation)
        assert all(
            instrumentation["full_projection_context_rows"][field]
            <= instrumentation["context_rows"][field]
            for field in instrumentation["context_rows"]
        )
        assert instrumentation["stage_duration_ms"]["projection_context_total"] >= (
            instrumentation["stage_duration_ms"].get("full_projection_context", 0.0)
        )
        assert cell["twin_semantics_equal"]
        assert cell["twin_decisions_equal"]
        assert cell["read_only_rebuild_digest_equal"]
        if cell["H"]:
            for stage in (
                "pre_commit_reduce_asset_facts",
                "writer_reduce_asset_facts",
            ):
                assert (
                    cell["unrelated_history_rows_by_stage"]["sqlite_materialized_rows"][
                        stage
                    ]
                    > 0
                )
                assert cell["unrelated_history_rows_by_stage"][
                    "store_returned_objects"
                ][stage]
        if cell["case"] == "asset_only":
            if has_candidate_facade:
                assert instrumentation["context_rows"] == {
                    "contracts": 0,
                    "assets": 0,
                    "runtime_records": 0,
                    "registrations": 0,
                    "claim_history_contract_ids": 0,
                }
                assert instrumentation["full_projection_context_calls"] == 0
                assert (
                    instrumentation["stage_sql_rows_fetched"].get(
                        "projection_context_total", 0
                    )
                    == 0
                )
                assert (
                    instrumentation["stage_decoded_rows"].get(
                        "projection_context_total", {}
                    )
                    == {}
                )
            else:
                assert instrumentation["full_projection_context_calls"] == 1
                assert (
                    instrumentation["context_rows"]
                    == (instrumentation["full_projection_context_rows"])
                )
        else:
            assert instrumentation["full_projection_context_calls"] == 1
            assert (
                instrumentation["context_rows"]
                == (instrumentation["full_projection_context_rows"])
            )
            if cell["H"]:
                assert (
                    cell["unrelated_history_rows_by_stage"]["sqlite_materialized_rows"][
                        "full_projection_context"
                    ]
                    > 0
                )
                assert cell["unrelated_history_rows_by_stage"][
                    "store_returned_objects"
                ]["full_projection_context"]

    probe = report["replay_probe"]
    assert probe["observation_kind"] == "separate_exact_replay_not_a_context_pair"
    assert probe["H"] == 16 and probe["P"] == 1
    assert probe["history_assets"] == 32
    assert probe["initial_publication_accepted"]
    assert probe["replay_accepted"]
    assert probe["store_unchanged_by_replay"]
    assert probe["expected_proof_runtime_record_rows"] == 4
    assert probe["expected_proof_roles"]["receipt_rows"] == 1
    assert probe["expected_proof_roles"]["head_rows"] == 1
    assert probe["expected_proof_roles"]["member_rows"] == 2
    assert probe["expected_proof_runtime_record_types"]
    _assert_context_entry_path(report, probe["instrumentation"])
    (summary,) = probe["context_loader_result_summaries"]
    assert summary["related_runtime_record_rows"] == 4
    assert (
        summary["related_runtime_record_ids_sha256"]
        == probe["expected_proof_record_ids_sha256"]
    )
    assert (
        summary["related_runtime_record_types"]
        == probe["expected_proof_runtime_record_types"]
    )
    if has_candidate_facade:
        assert probe["reader_path"] == "candidate_facade"
        assert summary["runtime_record_rows"] == 4
        assert probe["instrumentation"]["full_projection_context_calls"] == 0
    else:
        assert probe["reader_path"] == "contract_declaration"
        assert summary["runtime_record_rows"] > 4
        assert probe["instrumentation"]["full_projection_context_calls"] == 1
        assert (
            probe["instrumentation"]["context_rows"]["runtime_records"]
            == (
                probe["instrumentation"]["full_projection_context_rows"][
                    "runtime_records"
                ]
            )
        )
    _assert_counter_relationships(probe["instrumentation"])
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
    assert report["process_exit_code"] == result.returncode == 1
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
            "--context-compat",
            "--source-revision",
            "context-compat-manifest-mismatch",
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
    assert report["process_exit_code"] == result.returncode == 1
    assert report["failure_phase"] == "package_manifest"
    assert report["source"]["package_python_file_count"] >= 200
    assert report["source"]["package_manifest_sha256"] != "0" * 64
    assert report["completed_cells"] == 0
    assert report["parameters"]["mode"] == "context_compat"
    assert report["parameters"]["P"] == [1, 64]
    assert report["parameters"]["H"] == [0, 16, 64]
    assert report["parameters"]["modes"] == [
        "asset_only",
        "mixed_asset_declare",
    ]
    assert report["parameters"]["context_pairs"] == 6
    assert report["parameters"]["separate_replay_probe"]["timing_pair"] is False
    assert "/private/tmp" not in output.read_text()


def test_p95_uses_nearest_rank_for_small_samples() -> None:
    p95 = _SCRIPT_HELPERS["_nearest_rank_p95"]
    assert p95([1.0, 2.0, 3.0]) == 3.0
    assert p95([9.0]) == 9.0
