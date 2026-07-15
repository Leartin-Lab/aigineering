"""Executable constraints for the 0.5 design/change/evidence workflow."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_design_truth_and_active_change_are_present():
    design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
    change = (ROOT / "changes/001-candidate-genesis.md").read_text(encoding="utf-8")

    assert "Implemented runtime path" in design
    assert "Known transition boundaries".lower() in design.lower()
    for section in (
        "## Problem",
        "## Resulting design",
        "## Compatibility sequence",
        "## Required architecture tests",
        "## Deletion ledger",
        "## Exit criteria",
    ):
        assert section in change
    adr = (ROOT / "docs/adr/ADR-011-candidate-native-plugin-runtime.md").read_text(
        encoding="utf-8"
    )
    assert "Status: Accepted; migration in progress" in adr
    assert "current implemented truth" in adr


def test_legacy_runtime_files_stay_out_of_release_artifacts():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for path in (
        "src/aigineering/core/engine.py",
        "src/aigineering/core/startup_check.py",
        "src/aigineering/core/state_serializer.py",
    ):
        assert path in project


def test_contract_cli_uses_candidate_commitment_not_legacy_ingress():
    source = (ROOT / "src/aigineering/cli/contract.py").read_text(encoding="utf-8")

    assert "commit_local_effect" in source
    assert "contract_declaration_effect" in source
    assert "RuntimeIngress" not in source
    assert "inject_contract" not in source


def test_asset_add_uses_candidate_commitment_not_legacy_ingress():
    source = (ROOT / "src/aigineering/cli/asset.py").read_text(encoding="utf-8")
    add_body = source.split('@asset_group.command("ls")', 1)[0]

    assert "commit_local_effect" in add_body
    assert "asset_proposal_effect" in add_body
    assert "inject_asset" not in add_body
    assert "RuntimeIngress(" not in add_body


def test_asset_slice_uses_candidate_commitment_and_preserves_lineage():
    source = (ROOT / "src/aigineering/cli/asset.py").read_text(encoding="utf-8")
    body = source.split("def asset_slice", 1)[1].split(
        '@asset_group.command("replace")', 1
    )[0]

    assert "commit_local_effect" in body
    assert "asset_proposal_effect" in body
    assert "accept_asset" not in body
    projection = (ROOT / "src/aigineering/core/effect_projection.py").read_text(
        encoding="utf-8"
    )
    assert 'lineage_id=str(data.get("lineage_id", ""))' in projection


def test_task_create_uses_candidate_commitment_not_legacy_ingress():
    source = (ROOT / "src/aigineering/cli/task.py").read_text(encoding="utf-8")
    create_body = source.split('@task_group.command("status")', 1)[0]

    assert "commit_local_effect" in create_body
    assert "contract_declaration_effect" in create_body
    assert "inject_contract" not in create_body
    assert "RuntimeIngress(" not in create_body


def test_behavior_add_uses_asset_candidate_path():
    source = (ROOT / "src/aigineering/cli/behavior.py").read_text(encoding="utf-8")
    add_body = source.split('@behavior_group.command("list")', 1)[0]

    assert "commit_local_effect" in add_body
    assert "asset_proposal_effect" in add_body
    assert "inject_asset" not in add_body
    assert "RuntimeIngress(" not in add_body


def test_http_asset_and_contract_creation_require_signed_candidates():
    source = (ROOT / "src/aigineering/server/app.py").read_text(encoding="utf-8")
    creation_surface = source.split('@app.get("/contracts"', 1)[0]

    assert 'app.post("/candidates")' in creation_surface
    assert "CandidateProposalRequest" in creation_surface
    assert "CandidateCommitter" in creation_surface
    assert "ContractCreateRequest" not in source
    assert "AssetCreateRequest" not in source
    assert "inject_contract" not in creation_surface
    assert "inject_asset" not in creation_surface


def test_http_slice_recomputes_signed_candidate_payload_before_commit():
    source = (ROOT / "src/aigineering/server/app.py").read_text(encoding="utf-8")
    body = source.split("def slice_asset", 1)[1].split(
        '@app.post(\n    "/replacement-claims"', 1
    )[0]

    assert "AssetSliceCandidateRequest" in source
    assert "_require_single_effect" in body
    assert "asset_proposal_effect(expected)" in body
    assert "_commit_candidate_request" in body
    assert "RuntimeIngress" not in body


def test_recovery_recreate_publishes_contract_candidate():
    source = (ROOT / "src/aigineering/cli/recover.py").read_text(encoding="utf-8")
    recreate = source.split("def _recreate_contracts", 1)[1].split("@click.command", 1)[
        0
    ]

    assert "commit_local_effect" in recreate
    assert "contract_declaration_effect" in recreate
    assert "accept_contract" not in recreate
    assert "RuntimeIngress" not in recreate


def test_capability_and_mcp_descriptors_use_protected_asset_candidates():
    for relative in (
        "src/aigineering/cli/capability.py",
        "src/aigineering/cli/mcp.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "commit_local_effect" in source
        assert "asset_proposal_effect" in source
        assert "accept_asset" not in source
        assert "RuntimeIngress" not in source


def test_demo_bootstrap_publishes_all_ordinary_state_as_candidates():
    source = (ROOT / "src/aigineering/cli/_common.py").read_text(encoding="utf-8")
    demo = source.split("def _run_demo", 1)[1].split("def _redact_sealed", 1)[0]

    assert "commit_local_effect" in source
    assert "contract_declaration_effect" in demo
    assert "asset_proposal_effect" in demo
    assert "RuntimeIngress" not in demo
    assert "accept_contract" not in demo
    assert "accept_asset" not in demo


def test_effect_payload_builders_are_protocol_helpers_not_cli_semantics():
    source = (ROOT / "src/aigineering/protocol/effect_builders.py").read_text(
        encoding="utf-8"
    )

    assert "aigineering.cli" not in source
    assert "contract.declare" in source
    assert "asset.propose" in source


def test_skill_loader_builds_assets_and_cli_owns_candidate_publication():
    loader = (ROOT / "src/aigineering/core/skill_loader.py").read_text(encoding="utf-8")
    cli = (ROOT / "src/aigineering/cli/skill.py").read_text(encoding="utf-8")

    assert "RuntimeIngress" not in loader
    assert "accept_asset" not in loader
    assert "build_assets" in loader
    assert "commit_local_effect" in cli
    assert "asset_proposal_effect" in cli
    assert "RuntimeIngress" not in cli


def test_worker_registration_cli_uses_typed_candidate_effect():
    source = (ROOT / "src/aigineering/cli/worker.py").read_text(encoding="utf-8")
    register = source.split("def worker_register", 1)[1].split(
        '@worker.command("submit")', 1
    )[0]

    assert "worker_registration_effect" in register
    assert "commit_local_effect" in register
    assert "store.register_worker" not in register


def test_commitment_coordinator_does_not_own_effect_semantics():
    path = ROOT / "src/aigineering/core/commitment.py"
    source = path.read_text(encoding="utf-8")

    assert "asset.propose" not in source
    assert "contract.declare" not in source
    assert "worker_registration" not in source
    assert "scan_runtime_records" not in source
    assert len(source.splitlines()) < 300
