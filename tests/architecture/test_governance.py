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
        "src/aigineering/core/state_serializer.py",
    ):
        assert path in project

    assert "src/aigineering/core/startup_check.py" not in project
    assert not (ROOT / "src/aigineering/core/startup_check.py").exists()


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


def test_replacement_claim_cli_and_http_require_asset_relate_candidates():
    cli = (ROOT / "src/aigineering/cli/asset.py").read_text(encoding="utf-8")
    replace = cli.split("def asset_replace", 1)[1].split(
        '@asset_group.command("versions")', 1
    )[0]
    server = (ROOT / "src/aigineering/server/app.py").read_text(encoding="utf-8")
    endpoint = server.split("def create_replacement_claim", 1)[1].split(
        '@app.get("/replacement-claims"', 1
    )[0]

    assert "replacement_claim_effect" in replace
    assert "commit_local_effect" in replace
    assert "RuntimeIngress" not in replace
    assert "CandidateProposalRequest" in server
    assert '_require_single_effect(body, "asset.relate")' in endpoint
    assert "_commit_candidate_request" in endpoint
    assert "RuntimeIngress" not in endpoint


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


def test_recovery_cancel_uses_candidate_and_trace_is_not_task_state():
    source = (ROOT / "src/aigineering/cli/recover.py").read_text(encoding="utf-8")
    cancel = source.split("def _cancel_contracts", 1)[1].split(
        "def _recreate_contracts", 1
    )[0]

    assert "contract_cancellation_effect" in cancel
    assert "commit_local_effect" in cancel
    assert "RecoveryMethodHandler" not in source
    assert "MethodRuntime" not in source
    assert 'record_type="lifecycle.terminal"' in source


def test_retry_cli_publishes_an_ordinary_contract_candidate():
    source = (ROOT / "src/aigineering/cli/retry.py").read_text(encoding="utf-8")

    assert "contract_declaration_effect" in source
    assert "commit_local_effect" in source
    assert "causal_parents=(original.id,)" in source
    assert "MethodRuntime" not in source
    assert "RetryMethodHandler" not in source


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


def test_control_plane_is_proposal_construction_not_a_commit_path():
    source = (ROOT / "src/aigineering/core/control_plane.py").read_text(
        encoding="utf-8"
    )

    assert "build_control_plane_asset" in source
    assert "build_control_plane_contract" in source
    assert "RuntimeIngress" not in source
    assert "accept_asset" not in source
    assert "accept_contract" not in source
    assert "def inject_" not in source


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
    assert "actor_authorization_effect" in register
    assert "commit_local_effects" in register
    assert "store.register_worker" not in register


def test_actor_authorization_is_a_capability_gated_candidate_effect():
    projection = (ROOT / "src/aigineering/core/effect_projection.py").read_text(
        encoding="utf-8"
    )
    actor_facts = (ROOT / "src/aigineering/core/actor_facts.py").read_text(
        encoding="utf-8"
    )

    assert '"actor.authorize": ("actor.authorize"' in projection
    assert '"actor.revoke": ("actor.revoke"' in projection
    assert '"actor.rotate": ("actor.rotate"' in projection
    assert "actor.authorized" in actor_facts
    assert "actor.revoked" in actor_facts
    assert "validate_actor_authorization_record" in actor_facts
    assert "validate_candidate_receipt_actor" in actor_facts


def test_engine_worker_bootstraps_inner_domain_through_candidate_publication():
    source = (ROOT / "src/aigineering/agent/engine_worker.py").read_text(
        encoding="utf-8"
    )

    assert "initialize_genesis" in source
    assert "publish_effect" in source
    assert "asset_proposal_effect" in source
    assert "contract_declaration_effect" in source
    assert "RuntimeIngress" not in source
    assert "accept_asset" not in source
    assert "accept_contract" not in source
    assert "CandidatePublisherRegistry" in source
    assert "candidate_publishers=candidate_publishers" in source


def test_commitment_coordinator_does_not_own_effect_semantics():
    path = ROOT / "src/aigineering/core/commitment.py"
    source = path.read_text(encoding="utf-8")

    assert "asset.propose" not in source
    assert "contract.declare" not in source
    assert "worker_registration" not in source
    assert "scan_runtime_records" not in source
    assert "project_effect_batch" in source
    assert len(source.splitlines()) < 300


def test_worker_submission_uses_shared_fact_reduction_without_runtime_ingress():
    submit = (ROOT / "src/aigineering/core/submit.py").read_text(encoding="utf-8")
    runtime = (ROOT / "src/aigineering/runtime.py").read_text(encoding="utf-8")
    worker_cli = (ROOT / "src/aigineering/cli/worker.py").read_text(encoding="utf-8")
    submit_surface = runtime.split("def submit_candidate_envelope", 1)[1].split(
        "def _schedule_rejected_recovery", 1
    )[0]

    assert "reduce_asset_facts" in submit
    assert "RuntimeIngress" not in submit
    assert "RuntimeIngress" not in submit_surface
    assert "RuntimeIngress" not in worker_cli


def test_claim_bound_delegation_semantics_live_in_plugin_not_runtime_service():
    runtime = (ROOT / "src/aigineering/runtime.py").read_text(encoding="utf-8")
    worker = (ROOT / "src/aigineering/agent/worker.py").read_text(encoding="utf-8")
    plugin = (ROOT / "src/aigineering/plugins/delegation.py").read_text(
        encoding="utf-8"
    )

    assert "TaskDelegationPlugin().project" in runtime
    submission = runtime.split("def _submit_claimed_method", 1)[1].split(
        "def process_method_completions", 1
    )[0]
    assert "method_registry" not in submission
    assert ".can_handle(" not in submission
    assert "method_contract" not in runtime
    assert "retry_contract" not in runtime
    assert "method_context_content" not in runtime
    assert "TaskDelegationPlugin().propose" in worker
    assert "task_delegation_effect" in plugin
    assert "def project" in plugin
    assert "RuntimeIngress" not in plugin
    assert "self._store" not in plugin
    assert "accept_contract" not in plugin


def test_production_completion_projection_has_no_direct_ingress():
    runtime = (ROOT / "src/aigineering/runtime.py").read_text(encoding="utf-8")
    completion = runtime.split("def process_method_completions", 1)[1]

    assert "RuntimeIngress" not in completion
    assert "FactReducer" not in completion


def test_recovery_replay_requires_authenticated_candidate_publisher():
    runtime = (ROOT / "src/aigineering/runtime.py").read_text(encoding="utf-8")
    replay = runtime.split("def _schedule_rejected_recovery", 1)[1].split(
        "def process_rejected_submissions", 1
    )[0]

    assert "authenticated recovery Candidate publisher" in replay
    assert "candidate_publishers is None" in replay
    assert "RuntimeIngress" not in runtime
    assert "FactReducer" not in runtime


def test_projection_failure_terminal_is_distinct_from_recovery_progress():
    submit = (ROOT / "src/aigineering/core/submit.py").read_text(encoding="utf-8")
    runtime = (ROOT / "src/aigineering/runtime.py").read_text(encoding="utf-8")

    assert '"lifecycle.terminal"' in submit
    assert '"projection_rejection.recovery_scheduled"' in runtime
    assert "recovered_projection_ids" in runtime


def test_local_recovery_replay_publishes_contract_and_context_as_candidate():
    recovery = (ROOT / "src/aigineering/plugins/recovery.py").read_text(
        encoding="utf-8"
    )
    compatibility = (
        ROOT / "src/aigineering/core/method_handlers/recovery.py"
    ).read_text(encoding="utf-8")
    identity = (ROOT / "src/aigineering/local_identity.py").read_text(encoding="utf-8")

    assert 'can_publish_candidates("recovery.publish.v1")' in recovery
    assert "contract_declaration_effect(recovery)" in recovery
    assert "asset_proposal_effect(context_template)" in recovery
    assert '"recovery.publish.v1"' in identity
    assert '"contract.publish.protected"' in identity
    assert '"asset.publish.protected"' in identity
    assert "publish_task_effects" not in compatibility
    assert len(compatibility.splitlines()) < 60


def test_http_worker_ingress_never_provisions_local_private_keys_or_direct_recovery():
    server = (ROOT / "src/aigineering/server/app.py").read_text(encoding="utf-8")
    submit = server.split("def submit_worker_candidate", 1)[1].split(
        '@app.post("/contracts/{contract_id}/run"', 1
    )[0]

    assert "ensure_local_runtime_publishers" not in submit
    assert "RuntimeIngress" not in submit
    assert "process_rejected_submissions" not in submit


def test_local_identity_is_application_service_not_cli_implementation():
    compatibility = (ROOT / "src/aigineering/cli/identity.py").read_text(
        encoding="utf-8"
    )
    local = (ROOT / "src/aigineering/local_identity.py").read_text(encoding="utf-8")

    assert "from aigineering.local_identity import" in compatibility
    assert "aigineering.core" not in compatibility
    assert "aigineering.cli" not in local


def test_engine_worker_composes_authenticated_worker_and_completion_plugins():
    source = (ROOT / "src/aigineering/agent/engine_worker.py").read_text(
        encoding="utf-8"
    )
    hosting = (ROOT / "src/aigineering/worker_hosting.py").read_text(encoding="utf-8")

    assert "authorize_worker_host" in source
    assert "actor_authorization_effect" in hosting
    assert "worker_registration_effect" in hosting
    assert "WorkerHost" in hosting
    assert "default_completion_registry" in source
    assert "aigineering.application" not in source
    assert "MethodRegistry" not in source
    assert "method_handlers" not in source


def test_retry_delegation_does_not_ship_as_completion_registry_semantics():
    application = (ROOT / "src/aigineering/application.py").read_text(encoding="utf-8")
    plugins = (ROOT / "src/aigineering/plugins/__init__.py").read_text(encoding="utf-8")

    assert "RetryMethodHandler" not in application
    assert 'registry.register("retry"' not in plugins
    assert "default_method_registry" not in application
    assert "default_completion_registry" in application
    assert "MethodRegistry" not in application
    assert "CompletionRegistry" in plugins
    assert "PlanMethodHandler" not in application
    assert "ReplanMethodHandler" not in application
    assert "PlanningCompletionPlugin" in plugins
    assert "ReplanningCompletionPlugin" in plugins
    assert "ToolMethodHandler" not in application
    assert "ToolCompletionPlugin" in plugins
    assert "FailMethodHandler" not in application
    assert "FailCompletionPlugin" in plugins
