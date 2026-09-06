"""Executable constraints for the 0.5 design/change/evidence workflow."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def _public_markdown_files() -> tuple[Path, ...]:
    roots = (
        "README.md",
        "DESIGN.md",
        "ROADMAP.md",
        "CONTRIBUTING.md",
        "SKILL.md",
        "docs",
        "changes",
        "reports",
        "conformance",
    )
    files: list[Path] = []
    for relative in roots:
        path = ROOT / relative
        if path.is_file():
            files.append(path)
        else:
            files.extend(path.rglob("*.md"))
    return tuple(files)


def test_stable_design_change_and_adr_are_current():
    design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
    change = (ROOT / "changes/001-candidate-genesis.md").read_text(encoding="utf-8")

    for section in (
        "## Implemented runtime path",
        "## Known transition boundaries",
        "## Release limits",
    ):
        assert section in design
    for section in (
        "## Problem",
        "## Resulting design",
        "## Public compatibility",
        "## Required verification",
        "## Closure",
    ):
        assert section in change
    adr = (ROOT / "docs/adr/ADR-011-candidate-native-plugin-runtime.md").read_text(
        encoding="utf-8"
    )
    assert "Status: Accepted" in adr
    assert "migration in progress" not in adr
    assert "current implemented truth" in adr


def test_public_sources_do_not_reference_private_workspaces():
    roots = (
        "README.md",
        "DESIGN.md",
        "ROADMAP.md",
        "CONTRIBUTING.md",
        "SKILL.md",
        "docs",
        "changes",
        "reports",
        "src",
        "tests",
    )
    forbidden = ("." + "omo/", "." + "internal-docs", "internal " + "ADR")

    for relative in roots:
        path = ROOT / relative
        files = (path,) if path.is_file() else tuple(path.rglob("*"))
        for file in files:
            if (
                not file.is_file()
                or file.name == "AGENTS.md"
                or file.suffix not in {".md", ".py", ".toml"}
            ):
                continue
            content = file.read_text(encoding="utf-8")
            for marker in forbidden:
                assert marker not in content, (
                    f"{file.relative_to(ROOT)} contains {marker}"
                )


def test_documentation_index_routes_to_unique_public_owners():
    index = (ROOT / "docs/README.md").read_text(encoding="utf-8")

    for owner in (
        "../DESIGN.md",
        "boundary-invariants.md",
        "adr/",
        "../changes/",
        "../ROADMAP.md",
        "../reports/",
        "../conformance/",
        "../CONTRIBUTING.md",
        "../SKILL.md",
    ):
        assert f"]({owner})" in index


def test_public_skill_prefers_real_workers_and_routes_harness_migration():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "HarnessCandidateAdapter" in skill
    assert "docs/reference/agent-harness-migration.md" in skill
    assert 'aig run "produce a cited release review" --json' in skill
    assert "Never present mock output as production" in skill
    assert "--worker mock" in skill
    assert 'aig run "build a report with citations"' in readme
    quick_start = readme.split("## v0.5.4 scope", 1)[0]
    assert (
        "--worker mock"
        not in quick_start.split(
            "Mock execution is an explicit deterministic dry-run", 1
        )[0]
    )


def test_harness_adapter_delegates_to_the_workerhost_compiler():
    harness = (ROOT / "src/aigineering/agent/harness.py").read_text(encoding="utf-8")
    worker = (ROOT / "src/aigineering/agent/worker.py").read_text(encoding="utf-8")

    assert "compile_worker_envelope" in harness
    assert "claim_bound_graph_output_effects" not in harness
    assert worker.count("def compile_worker_envelope(") == 1
    assert worker.count("claim_bound_graph_output_effects(") == 1


def test_public_markdown_local_links_resolve():
    link_pattern = re.compile(r"(?<!!)\[[^]]*]\(([^)]+)\)")

    for source in _public_markdown_files():
        content = source.read_text(encoding="utf-8")
        for raw_target in link_pattern.findall(content):
            target = raw_target.split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (source.parent / target).resolve()
            assert resolved.exists(), (
                f"{source.relative_to(ROOT)} links to missing {raw_target}"
            )


def test_released_changes_are_ordered_and_design_matches_package_version():
    roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    redis_change = (ROOT / "changes/003-redis-query-projection.md").read_text(
        encoding="utf-8"
    )
    identity_change = (
        ROOT / "changes/004-signed-definition-content-graph.md"
    ).read_text(encoding="utf-8")
    redis_adr = (
        ROOT / "docs/adr/ADR-016-disposable-redis-query-projection.md"
    ).read_text(encoding="utf-8")
    identity_adr = (
        ROOT / "docs/adr/ADR-017-signed-definition-content-graph.md"
    ).read_text(encoding="utf-8")
    design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")

    assert roadmap.index("v0.5.4") < roadmap.index("Future candidate directions")
    assert "Status: Implemented and verified" in redis_change
    assert "Change 003 is closed" in identity_change
    assert "Status: Accepted" in redis_adr
    assert "Status: Implemented and verified" in identity_change
    assert "Status: Accepted" in identity_adr
    from aigineering import __version__

    assert f"Status: implemented truth for v{__version__}" in design
    assert "Redis projection" in design
    assert "## Asset identity graph" in design


def test_redis_adapter_cannot_enter_correctness_owners():
    forbidden = (
        "src/aigineering/core/commitment.py",
        "src/aigineering/core/authority.py",
        "src/aigineering/core/causal_allowance.py",
        "src/aigineering/core/claims.py",
        "src/aigineering/core/acceptance.py",
        "src/aigineering/core/lifecycle_facts.py",
        "src/aigineering/core/fact_reducer.py",
    )

    for relative in forbidden:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "redis" not in source.lower()
        assert "query_projection" not in source


def test_legacy_runtime_files_stay_out_of_release_artifacts():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "src/aigineering/core/runtime_ingress.py" not in project
    for path in (
        "engine.py",
        "context_overflow.py",
        "method_registry.py",
        "method_runtime.py",
        "continuation_manager.py",
        "state_serializer.py",
        "runtime_ingress.py",
    ):
        assert not (ROOT / "src/aigineering/core" / path).exists()
    assert not list((ROOT / "src/aigineering/core/method_handlers").glob("*.py"))

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
    projection = (ROOT / "src/aigineering/core/effect_projectors.py").read_text(
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
    projection = (ROOT / "src/aigineering/core/effect_projectors.py").read_text(
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
    commitment = (ROOT / "src/aigineering/core/commitment.py").read_text(
        encoding="utf-8"
    )
    runtime = (ROOT / "src/aigineering/runtime.py").read_text(encoding="utf-8")
    worker_cli = (ROOT / "src/aigineering/cli/worker.py").read_text(encoding="utf-8")

    assert not (ROOT / "src/aigineering/core/submit.py").exists()
    assert "reduce_asset_facts" in commitment
    assert "def submit_candidate_envelope" not in runtime
    assert "def _submit_claimed_method" not in runtime
    assert "CandidateCommitter(store, trace).commit(proposal)" in runtime
    assert "RuntimeIngress" not in worker_cli


def test_store_has_one_generic_candidate_commit_transaction():
    store = (ROOT / "src/aigineering/core/sqlite_store.py").read_text(encoding="utf-8")
    protocol = (ROOT / "src/aigineering/core/store.py").read_text(encoding="utf-8")

    assert "def commit_ingress_batch" in store
    assert "def commit_candidate_submission" not in store
    assert "def commit_method_submission" not in store
    assert "commit_candidate_submission" not in protocol
    assert "commit_method_submission" not in protocol


def test_claim_bound_delegation_semantics_live_in_plugin_not_runtime_service():
    runtime = (ROOT / "src/aigineering/runtime.py").read_text(encoding="utf-8")
    worker = (ROOT / "src/aigineering/agent/worker.py").read_text(encoding="utf-8")
    plugin = (ROOT / "src/aigineering/plugins/delegation.py").read_text(
        encoding="utf-8"
    )

    assert "TaskDelegationPlugin().project" not in runtime
    assert "task.delegate" not in runtime
    assert "method_contract" not in runtime
    assert "retry_contract" not in runtime
    assert "method_context_content" not in runtime
    assert "TaskDelegationPlugin()" in worker
    assert ".propose_claimed(" in worker
    assert "task_delegation_effect" not in plugin
    assert "def project" not in plugin
    assert "RuntimeIngress" not in plugin
    assert "self._store" not in plugin
    assert "accept_contract" not in plugin


def test_production_completion_projection_has_no_direct_ingress():
    runtime = (ROOT / "src/aigineering/runtime.py").read_text(encoding="utf-8")
    completion = runtime.split("def process_task_completions", 1)[1].split(
        "def _method_context_assets_for", 1
    )[0]

    assert "RuntimeIngress" not in completion
    assert "FactReducer" not in completion
    assert "commit_ingress_batch" in completion
    assert ".append_runtime_record(" not in completion
    assert "def process_method_completions" not in runtime
    projector = (ROOT / "src/aigineering/plugins/completion_projection.py").read_text(
        encoding="utf-8"
    )
    assert "plugin:plan.compile" not in projector
    assert "plugin:replan.compile" not in projector


def test_server_delegates_worker_command_authentication_to_protocol_service():
    server = (ROOT / "src/aigineering/server/app.py").read_text(encoding="utf-8")
    coordination = (ROOT / "src/aigineering/core/worker_coordination.py").read_text(
        encoding="utf-8"
    )

    assert "authenticate_worker_command" in server
    assert "candidate_received_record" not in server
    assert "load_effective_actor_keys" not in server
    assert "worker command actor lacks" in coordination


def test_production_loops_use_neutral_task_completion_entrypoint():
    cli = (ROOT / "src/aigineering/cli/run.py").read_text(encoding="utf-8")
    nested = (ROOT / "src/aigineering/agent/engine_worker.py").read_text(
        encoding="utf-8"
    )

    assert "process_task_completions" in cli
    assert "process_task_completions" in nested
    assert "process_method_completions" not in cli
    assert "process_method_completions" not in nested


def test_new_expansion_avoids_delegation_facts_and_old_facts_remain_readable():
    runtime = (ROOT / "src/aigineering/runtime.py").read_text(encoding="utf-8")
    plugin = (ROOT / "src/aigineering/plugins/delegation.py").read_text(
        encoding="utf-8"
    )
    projection = (ROOT / "src/aigineering/core/runtime_projection.py").read_text(
        encoding="utf-8"
    )

    assert 'create_runtime_record(\n        "task.delegated"' not in runtime
    assert '"status": "task_delegated"' in runtime
    assert 'CandidateEffect("task.delegate"' not in plugin
    assert '"task_delegated"' in projection
    assert '"method_scheduled"' in projection


def test_runtime_task_state_is_a_pure_boolean_projection():
    projection = (ROOT / "src/aigineering/core/runtime_projection.py").read_text(
        encoding="utf-8"
    )
    task_state = (ROOT / "src/aigineering/cli/task_state.py").read_text(
        encoding="utf-8"
    )

    assert 'blockers.append("delegation_pending")' in projection
    assert "method_pending" not in projection
    assert 'return "blocked_delegation"' in task_state
    assert ".add_contract(" not in projection
    assert ".add_asset(" not in projection
    assert ".append_runtime_record(" not in projection


def test_recovery_replay_requires_authenticated_candidate_publisher():
    runtime = (ROOT / "src/aigineering/runtime.py").read_text(encoding="utf-8")
    replay = runtime.split("def _schedule_rejected_recovery", 1)[1].split(
        "def process_rejected_submissions", 1
    )[0]

    assert "authenticated recovery Candidate publisher" in replay
    assert ".append_runtime_record(" not in runtime
    assert "def _commit_recovery_outcome" in runtime
    assert "store.commit_ingress_batch(" in runtime
    assert "candidate_publishers is None" in replay
    assert "RuntimeIngress" not in runtime
    assert "FactReducer" not in runtime


def test_projection_failure_terminal_is_distinct_from_recovery_progress():
    projection = (ROOT / "src/aigineering/core/effect_projectors.py").read_text(
        encoding="utf-8"
    )
    runtime = (ROOT / "src/aigineering/runtime.py").read_text(encoding="utf-8")

    assert "create_terminal_record" in projection
    assert '"projection_rejection.recovery_scheduled"' in runtime
    assert "recovered_source_ids" in runtime
    assert '"candidate_rejection.recovery_scheduled"' in runtime


def test_local_recovery_replay_publishes_contract_and_context_as_candidate():
    recovery = (ROOT / "src/aigineering/plugins/recovery.py").read_text(
        encoding="utf-8"
    )
    identity = (ROOT / "src/aigineering/local_identity.py").read_text(encoding="utf-8")

    assert 'can_publish_candidates("recovery.publish.v1")' in recovery
    assert "contract_declaration_effect(recovery)" in recovery
    assert "asset_proposal_effect(context_template)" in recovery
    assert '"recovery.publish.v1"' in identity
    assert '"contract.publish.protected"' in identity
    assert '"asset.publish.protected"' in identity
    assert "runtime.add_contract" not in recovery
    assert "mint_authorized_system_asset" not in recovery
    assert not (ROOT / "src/aigineering/core/method_handlers/recovery.py").exists()


def test_completion_plugins_have_no_direct_contract_mutation_fallback():
    completion = (ROOT / "src/aigineering/plugins/completion_projection.py").read_text(
        encoding="utf-8"
    )
    planning = (ROOT / "src/aigineering/plugins/planning_completion.py").read_text(
        encoding="utf-8"
    )

    assert "def add_contract" not in completion
    assert "mint_authorized_system_asset" not in completion
    assert "runtime.add_contract" not in planning


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


def test_task_projection_semantics_live_with_plugins_not_core_compatibility():
    compatibility = (ROOT / "src/aigineering/core/methods.py").read_text(
        encoding="utf-8"
    )
    semantics = (ROOT / "src/aigineering/plugins/task_semantics.py").read_text(
        encoding="utf-8"
    )
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for directory in ("plugins", "agent")
        for path in (ROOT / f"src/aigineering/{directory}").glob("*.py")
    )

    assert "def contracts_from_plan_asset" in semantics
    assert "def method_contract" in semantics
    assert "def continuation_contract" in semantics
    assert "aigineering.core.methods" not in production
    assert len(compatibility.splitlines()) < 30

    core_plugin_imports = {
        path.name
        for path in (ROOT / "src/aigineering/core").glob("*.py")
        if path.name != "methods.py"
        and "aigineering.plugins" in path.read_text(encoding="utf-8")
    }
    assert core_plugin_imports == set()


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


def test_productivity_audit_and_tool_worker_preserve_candidate_boundary():
    """Read projections and tool adapters cannot become alternate ingress paths."""
    productivity = (ROOT / "src/aigineering/core/task_productivity.py").read_text(
        encoding="utf-8"
    )
    assert "aigineering.plugins" not in productivity
    for forbidden in (
        "CandidateCommitter",
        "RuntimeIngress",
        "commit_ingress_batch",
        "append_runtime_record",
        ".add_asset(",
        ".add_contract(",
    ):
        assert forbidden not in productivity

    task = (ROOT / "src/aigineering/cli/task.py").read_text(encoding="utf-8")
    audit = task.split("def task_audit", 1)[1].split("def _emit_error", 1)[0]
    assert "project_task_productivity" in audit
    for forbidden in (
        "commit_local_effect",
        "commit_ingress_batch",
        "append_runtime_record",
        ".add_asset(",
        ".add_contract(",
    ):
        assert forbidden not in audit

    for relative in (
        "src/aigineering/agent/tool_worker.py",
        "src/aigineering/agent/tool_executor.py",
    ):
        worker = (ROOT / relative).read_text(encoding="utf-8")
        assert "Candidate" in worker
        for forbidden in (
            "CandidateCommitter",
            "RuntimeIngress",
            "commit_ingress_batch",
            "append_runtime_record",
            ".add_asset(",
            ".add_contract(",
        ):
            assert forbidden not in worker
