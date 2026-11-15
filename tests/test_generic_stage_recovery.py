"""Generic staged-planning recovery stays on signed Task/Asset boundaries."""

from __future__ import annotations

import json

import pytest

from conftest import hosted_worker

from aigineering.agent.worker import WorkerExecutionError
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.domain import initialize_genesis
from aigineering.core.output_satisfaction import selected_output_assets
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins import PluginRequest, StagedPlanningPlugin
from aigineering.plugins.task_semantics import system_asset
from aigineering.protocol.candidate import (
    ActorKey,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_cancellation_effect,
    contract_declaration_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.types import Asset, Candidate
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    process_worker_failures,
)


_RECOVERY_CAPABILITIES = (
    "asset.publish",
    "asset.publish.protected",
    "contract.publish",
    "contract.publish.protected",
)
_SETUP_CAPABILITIES = (
    "actor.authorize",
    "actor.revoke",
    "asset.publish",
    "asset.publish.protected",
    "contract.cancel",
    "contract.publish",
    "contract.publish.protected",
    "worker.register",
)
_DATA_NAMES = ("co2_data", "study_spec")


class _StageWorker:
    """Exercise WorkerHost with one typed malformed-action failure, no provider."""

    worker_id = "worker:noaa-stage-recovery"

    def __init__(self, failed_contract_id: str, repair_content: str):
        self.failed_contract_id = failed_contract_id
        self.repair_content = repair_content
        self.failed_once = False

    def registration(self):
        return WorkerRegistration(self.worker_id, pools=("planning",))

    def invoke(self, contract, _disclosed_assets):
        if contract.id == self.failed_contract_id and not self.failed_once:
            self.failed_once = True
            raise WorkerExecutionError(
                "malformed_json_action",
                "LLM response was not one valid canonical JSON action",
                usage_metadata={"prompt_tokens": 11, "completion_tokens": 7},
            )
        if contract.origin == "recovery":
            if "plugin:plan.compile" in contract.labels:
                raw = "/exec " + json.dumps(
                    {
                        "outputs": {
                            "planning_blueprint": json.dumps(
                                {
                                    "contracts": [
                                        {
                                            "name": "reporter",
                                            "description": "Publish the admitted report.",
                                            "inputs": list(_DATA_NAMES),
                                            "outputs": ["co2_report"],
                                            "budget": 1,
                                            "tool_scope": [],
                                            "capability_needs": [],
                                            "pool_needs": [],
                                        }
                                    ]
                                },
                                sort_keys=True,
                            )
                        }
                    },
                    sort_keys=True,
                )
                return Candidate(worker_id=self.worker_id, raw_output=raw)
            content = self.repair_content
        elif "plugin:plan.draft" in contract.labels:
            content = json.dumps(
                {
                    "goals": ["map NOAA evidence"],
                    "evidence_needs": ["co2_data", "study_spec"],
                    "uncertainties": [],
                    "proposed_steps": ["analyze", "report"],
                },
                sort_keys=True,
            )
        elif "plugin:plan.dependencies" in contract.labels:
            content = self.repair_content
        else:
            raise AssertionError(f"unexpected Worker invocation: {contract.name}")
        raw = "/exec " + json.dumps(
            {"outputs": {contract.outputs[0]: content}}, sort_keys=True
        )
        return Candidate(worker_id=self.worker_id, raw_output=raw)


def _world(
    tmp_path,
    *,
    root_budget: int = 7,
    failed_stage: str = "dependencies",
    recovery_capabilities: tuple[str, ...] | None = _RECOVERY_CAPABILITIES,
    include_recovery_key: bool = True,
):
    store = SQLiteStore(tmp_path / "generic-stage-recovery.sqlite")
    setup_signer = Ed25519Signer()
    setup_key = ActorKey(
        "human:noaa-recovery-test",
        "key:setup",
        setup_signer.kind,
        setup_signer.signer_id,
        _SETUP_CAPABILITIES,
    )
    recovery_signer = Ed25519Signer()
    recovery_key = ActorKey(
        "plugin:recovery.publish.v1",
        "key:recovery",
        recovery_signer.kind,
        recovery_signer.signer_id,
        recovery_capabilities or _RECOVERY_CAPABILITIES,
    )
    genesis_keys = (setup_key, recovery_key) if include_recovery_key else (setup_key,)
    genesis = create_genesis_manifest(
        "generic-stage-recovery", genesis_keys, "policy:generic-stage-recovery"
    )
    initialize_genesis(store, genesis)
    setup = CandidatePublisher(store, store, genesis, setup_key, setup_signer)

    data_assets = tuple(
        Asset(
            id=f"asset:test:{name}",
            name=name,
            content=f"frozen {name}",
            created_by=setup_key.actor_id,
        )
        for name in _DATA_NAMES
    )
    for asset in data_assets:
        decision = setup.publish(
            (asset_proposal_effect(asset),),
            idempotency_key=f"input:{asset.name}",
        )
        assert decision.accepted

    root = contract_from_fields(
        name="noaa_root",
        description="Bounded NOAA reanalysis root.",
        inputs=_DATA_NAMES,
        outputs=("co2_report",),
        activation="",
        budget=root_budget,
        tool_scope=(),
        labels=(),
        worker_pools=("planning",),
        delegation_pools=("planning", "research", "verification"),
        origin="human",
        acceptance_policy={"mode": "mechanical", "required_attestations": 1},
    )
    root_decision = setup.publish(
        (contract_declaration_effect(root),), idempotency_key="declare:noaa-root"
    )
    assert root_decision.accepted

    stages = StagedPlanningPlugin().stages(PluginRequest(parent=root, allowance=7))
    draft, dependencies, compile_task = stages.contracts
    failed = dependencies if failed_stage == "dependencies" else compile_task
    declarations = setup.publish(
        tuple(contract_declaration_effect(task) for task in stages.contracts),
        idempotency_key="declare:noaa-stages",
    )
    assert declarations.accepted
    repair_document = {
        "producers": [{"task": "analysis", "output": "co2_report"}],
        "consumers": [{"task": "analysis", "input": _DATA_NAMES[0]}],
        "missing_inputs": [],
        "cycles": [],
        "parallel_groups": [],
        "capability_needs": [],
        "authority_risks": [],
        "allowance_needs": [],
    }
    repair_content = json.dumps(repair_document, sort_keys=True)
    worker = _StageWorker(failed.id, repair_content)
    host = hosted_worker(
        store,
        worker,
        genesis=genesis,
        authority_key=setup_key,
        authority_signer=setup_signer,
    )
    recovery_publisher = CandidatePublisher(
        store, store, genesis, recovery_key, recovery_signer
    )
    publishers = CandidatePublisherRegistry(
        (("recovery.publish.v1", recovery_publisher),)
    )
    return {
        "store": store,
        "setup": setup,
        "setup_key": setup_key,
        "genesis": genesis,
        "root": root,
        "draft": draft,
        "dependencies": dependencies,
        "failed": failed,
        "compile": compile_task,
        "worker": worker,
        "host": host,
        "recovery_key": recovery_key,
        "recovery_signer": recovery_signer,
        "recovery_publisher": recovery_publisher,
        "publishers": publishers,
        "repair_content": repair_content,
    }


def _claim(env, contract, suffix: str, *, publishers=None):
    return claim_next_package(
        env["store"],
        worker_id=env["host"].worker_id,
        contract_id=contract.id,
        candidate_publishers=publishers or env["publishers"],
        claim_candidate=env["host"].claim_candidate(
            request_id=f"claim:{suffix}", contract_id=contract.id
        ),
        expected_registration_version=env["host"].registration_version,
    )


def _fail_dependency_stage(env, *, publishers=None):
    draft_claim = _claim(env, env["draft"], "draft", publishers=publishers)
    assert draft_claim is not None, (
        RuntimeProjection(env["store"], env["store"]).contract_view(env["draft"]),
        env["store"].get_worker_registration(env["host"].worker_id),
    )
    draft_result = execute_claimed_package(
        draft_claim,
        env["host"],
        env["store"],
        candidate_publishers=publishers or env["publishers"],
    )
    assert draft_result["status"] == "accepted"

    if env["failed"].id == env["compile"].id:
        dependencies_claim = _claim(
            env, env["dependencies"], "dependencies", publishers=publishers
        )
        assert dependencies_claim is not None
        dependencies_result = execute_claimed_package(
            dependencies_claim,
            env["host"],
            env["store"],
            candidate_publishers=publishers or env["publishers"],
        )
        assert dependencies_result["status"] == "accepted"

    failed_claim = _claim(env, env["failed"], "failed", publishers=publishers)
    assert failed_claim is not None
    with pytest.raises(WorkerInvocationError, match="recovery was evaluated"):
        execute_claimed_package(
            failed_claim,
            env["host"],
            env["store"],
            candidate_publishers=publishers or env["publishers"],
        )
    failure = next(
        record
        for _revision, record in reversed(env["store"].scan_runtime_records())
        if record.record_type == "worker.invocation_failed"
        and record.payload.get("contract_id") == env["failed"].id
        and record.payload.get("claim_id") == failed_claim.package.claim_id
    )
    return failed_claim, failure


def test_malformed_dependency_invocation_recovers_as_funded_ordinary_work(tmp_path):
    env = _world(tmp_path)
    store = env["store"]
    before_records = tuple(record for _, record in store.scan_runtime_records())
    assert allowance_available(env["root"], before_records) == 3
    assert "plan_draft" not in env["root"].inputs
    assert env["failed"].inputs == (env["draft"].outputs[0],)

    failed_claim, failure = _fail_dependency_stage(env)
    assert failure.payload["category"] == "worker_error:malformed_json_action"
    assert store.get_claim(env["failed"].id)["status"] == "released"
    extinguished = [
        record
        for _revision, record in store.scan_runtime_records(
            record_type="allowance.extinguished"
        )
        if record.payload.get("contract_id") == env["failed"].id
    ]
    assert len(extinguished) == 1
    assert extinguished[0].payload["amount"] == 1
    assert extinguished[0].payload["reason"] == "worker.invocation_failed"

    recovery = next(
        contract
        for contract in store.get_all_contracts()
        if contract.origin == "recovery"
        and contract.description
        and json.loads(contract.description).get("failed_contract_id")
        == env["failed"].id
    )
    recovery_description = json.loads(recovery.description)
    failed_description = json.loads(env["failed"].description)
    assert all(
        recovery_description[key] == value for key, value in failed_description.items()
    )
    assert recovery_description["recovery"] == "projection_repair"
    assert recovery.outputs == env["failed"].outputs
    assert recovery.inputs == (
        f"_fail_context_{env['failed'].id}",
        *env["failed"].inputs,
    )
    assert recovery.tool_scope == env["failed"].tool_scope
    assert recovery.worker_capabilities == env["failed"].worker_capabilities
    assert recovery.worker_pools == env["failed"].worker_pools
    assert recovery.delegation_capabilities == env["failed"].delegation_capabilities
    assert recovery.delegation_pools == env["failed"].delegation_pools
    assert recovery.context_asset_ids == env["failed"].context_asset_ids
    assert recovery.sensitive_input_policy == env["failed"].sensitive_input_policy
    assert recovery.acceptance_policy == env["failed"].acceptance_policy
    assert recovery.parent_id == env["root"].id
    context = store.get_assets_by_name(f"_fail_context_{env['failed'].id}")[0]

    recovery_publications = [
        record
        for _revision, record in store.scan_runtime_records(
            record_type="candidate.received"
        )
        if record.payload.get("actor_id") == "plugin:recovery.publish.v1"
    ]
    assert len(recovery_publications) == 1
    recovery_candidate_id = recovery_publications[0].payload["candidate_id"]
    recovery_head = next(
        record
        for _revision, record in store.scan_runtime_records(
            record_type="candidate.committed"
        )
        if record.payload.get("candidate_id") == recovery_candidate_id
    )
    committed_members = {
        store.get_runtime_record(member_id).record_type
        for member_id in recovery_head.payload["committed_record_ids"]
    }
    assert "contract.declared" in committed_members
    assert "asset.committed" in committed_members
    assert any(
        record.record_type == "allowance.reserved"
        and record.payload.get("source_contract_id") == env["root"].id
        and record.payload.get("child_contract_id") == recovery.id
        and record.payload.get("amount") == 1
        for _revision, record in store.scan_runtime_records()
    )
    assert (
        allowance_available(
            env["root"], tuple(record for _, record in store.scan_runtime_records())
        )
        == 2
    )

    repair_claim = _claim(env, recovery, "recovery")
    assert repair_claim is not None
    assert any(asset.id == context.id for asset in repair_claim.disclosed_assets)
    repair_result = execute_claimed_package(
        repair_claim, env["host"], store, candidate_publishers=env["publishers"]
    )
    assert repair_result["status"] == "accepted"
    repaired_asset = store.get_assets_by_name(env["failed"].outputs[0])[0]
    assert repaired_asset.content == env["repair_content"]
    assert store.get_claim(recovery.id)["status"] == "submitted"

    view = RuntimeProjection(store, store)
    assert view.contract_view(env["failed"]).terminal == "complete"
    assert view.contract_view(env["compile"]).enabled
    assert _claim(env, env["compile"], "compile") is not None
    assert selected_output_assets(env["root"], store).get("co2_report") is None
    assert view.contract_view(env["root"]).terminal is None

    revision = store.get_runtime_revision()
    contracts_before = tuple(store.get_all_contracts())
    receipts_before = tuple(
        record.id
        for _row_revision, record in store.scan_runtime_records(
            record_type="candidate.received"
        )
    )
    assert process_worker_failures(store, candidate_publishers=env["publishers"]) == []
    assert store.get_runtime_revision() == revision
    assert tuple(store.get_all_contracts()) == contracts_before
    assert (
        tuple(
            record.id
            for _row_revision, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
        )
        == receipts_before
    )
    assert failed_claim.package.claim_id == failure.payload["claim_id"]
    store.close()


def test_compile_recovery_retains_allowed_inputs_and_parent_acceptance_policy(
    tmp_path,
):
    env = _world(tmp_path, failed_stage="compile")
    store = env["store"]
    compile_description = json.loads(env["compile"].description)
    assert compile_description["allowed_inputs"] == list(_DATA_NAMES)
    assert (
        compile_description["parent_acceptance_policy"] == env["root"].acceptance_policy
    )

    _failed_claim, failure = _fail_dependency_stage(env)
    assert failure.payload["category"] == "worker_error:malformed_json_action"
    recovery = next(
        contract
        for contract in store.get_all_contracts()
        if contract.origin == "recovery"
        and json.loads(contract.description).get("failed_contract_id")
        == env["compile"].id
    )
    recovered_description = json.loads(recovery.description)
    assert all(
        recovered_description[key] == value
        for key, value in compile_description.items()
    )
    assert recovered_description["mode"] == "plan"
    assert recovered_description["stage"] == "compile"
    assert recovered_description["allowed_inputs"] == list(_DATA_NAMES)
    assert (
        recovered_description["parent_acceptance_policy"]
        == env["root"].acceptance_policy
    )
    assert recovery.acceptance_policy == env["compile"].acceptance_policy
    assert recovery.inputs == (
        f"_fail_context_{env['compile'].id}",
        *env["compile"].inputs,
    )
    assert store.get_claim(env["compile"].id)["status"] == "released"
    assert selected_output_assets(env["root"], store).get("co2_report") is None
    store.close()


def test_plain_task_recovery_retains_original_description_text(tmp_path):
    env = _world(tmp_path)
    store = env["store"]
    source_description = "Plain source task instructions must survive recovery."
    plain = contract_from_fields(
        name="plain_task",
        description=source_description,
        parent_id=env["root"].id,
        inputs=_DATA_NAMES,
        outputs=("plain_output",),
        activation="",
        budget=1,
        tool_scope=(),
        labels=(),
        worker_pools=("planning",),
        origin="human",
    )
    declaration = env["setup"].publish(
        (contract_declaration_effect(plain),), idempotency_key="declare:plain-task"
    )
    assert declaration.accepted
    env["worker"].failed_contract_id = plain.id
    env["failed"] = plain

    _failed_claim, failure = _fail_dependency_stage(env)
    assert failure.payload["category"] == "worker_error:malformed_json_action"
    recovery = next(
        contract
        for contract in store.get_all_contracts()
        if contract.origin == "recovery"
        and json.loads(contract.description).get("failed_contract_id") == plain.id
    )
    recovered_description = json.loads(recovery.description)
    assert recovered_description["source_description"] == source_description
    assert recovered_description["recovery"] == "projection_repair"
    assert recovery.inputs == (f"_fail_context_{plain.id}", *_DATA_NAMES)
    assert recovery.outputs == plain.outputs
    assert recovery.acceptance_policy == plain.acceptance_policy
    store.close()


def test_legacy_generic_recovery_replay_returns_exact_id_without_maker(
    tmp_path, monkeypatch
):
    env = _world(tmp_path)
    store = env["store"]
    empty_publishers = CandidatePublisherRegistry(())
    _failed_claim, failure = _fail_dependency_stage(env, publishers=empty_publishers)
    failed = env["failed"]
    funder = env["root"]
    source_record_ids = (failure.id,)
    context_name = f"_fail_context_{failed.id}"
    repair_instructions = (
        "Return exactly /exec with only the failed contract's declared outputs. "
        "Do not invent undeclared asset names. Use the rejection reasons to "
        "correct the output format and names."
    )
    old_description = json.dumps(
        {
            "recovery": "projection_repair",
            "failed_contract_id": failed.id,
            "source_record_ids": list(source_record_ids),
            "instructions": (
                f"Read {context_name}. {repair_instructions} Required outputs: "
                f"{list(failed.outputs)}."
            ),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    old_recovery = contract_from_fields(
        name=f"{failed.name or failed.id}.recover",
        description=old_description,
        inputs=(context_name, *failed.inputs),
        outputs=failed.outputs,
        activation=f"({failed.activation}) AND {context_name}",
        budget=1,
        tool_scope=failed.tool_scope,
        labels=failed.labels,
        worker_capabilities=failed.worker_capabilities,
        worker_pools=failed.worker_pools,
        delegation_capabilities=failed.delegation_capabilities,
        delegation_pools=failed.delegation_pools,
        origin="recovery",
        parent_id=funder.id,
        minting_authority=(context_name,),
        sensitive_input_policy=failed.sensitive_input_policy,
        acceptance_policy=failed.acceptance_policy,
        context_asset_ids=failed.context_asset_ids,
    )
    context = system_asset(
        name=context_name,
        content=json.dumps(
            {
                "trigger": "projection_rejected",
                "failed_contract_id": failed.id,
                "failed_contract_name": failed.name,
                "bad_worker_output": "provider invocation failed before Candidate production",
                "rejections": [
                    {
                        "category": failure.payload["category"],
                        "name": "(provider)",
                        "reject_reason": failure.payload["diagnostic"],
                    }
                ],
                "required_outputs": list(failed.outputs),
                "instructions": repair_instructions,
                "source_record_ids": list(source_record_ids),
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
        created_by=funder.id,
        promptable=True,
    )
    old_publication = env["recovery_publisher"].publish(
        (
            contract_declaration_effect(old_recovery),
            asset_proposal_effect(context),
        ),
        idempotency_key=f"projection-recovery:{failed.id}",
        causal_parents=(failed.id, *source_record_ids),
    )
    assert old_publication.accepted
    assert old_publication.contracts == (old_recovery,)
    assert old_publication.assets[0].name == context_name

    # Spend the remaining real funder balance. Exact old publication replay
    # must still win before the new recovery maker or funding selection runs.
    for index in range(2):
        filler = contract_from_fields(
            name=f"funder_reserve_{index}",
            description="Consume a real causal allowance unit.",
            parent_id=funder.id,
            inputs=funder.inputs,
            outputs=(f"funder_reserve_output_{index}",),
            activation="",
            budget=1,
            tool_scope=(),
            labels=(),
            origin="human",
        )
        result = env["setup"].publish(
            (contract_declaration_effect(filler),),
            idempotency_key=f"declare:funder-reserve:{index}",
        )
        assert result.accepted
    assert (
        allowance_available(
            funder, tuple(record for _, record in store.scan_runtime_records())
        )
        == 0
    )

    import aigineering.plugins.recovery as recovery_module

    def unexpected_maker(*_args, **_kwargs):
        raise AssertionError("old recovery replay invoked the new effect maker")

    monkeypatch.setattr(
        recovery_module, "_projection_recovery_intent", unexpected_maker
    )
    revision = store.get_runtime_revision()
    contracts_before = tuple(store.get_all_contracts())
    assert process_worker_failures(store, candidate_publishers=env["publishers"]) == []
    assert store.get_runtime_revision() == revision
    assert tuple(store.get_all_contracts()) == contracts_before
    assert store.get_contract(old_recovery.id) == old_recovery
    store.close()


def test_generic_stage_recovery_does_not_create_work_without_causal_funds(tmp_path):
    env = _world(tmp_path, root_budget=4)
    store = env["store"]
    before = tuple(record for _, record in store.scan_runtime_records())
    assert allowance_available(env["root"], before) == 0
    _failed_claim, failure = _fail_dependency_stage(env)
    assert failure.payload["category"] == "worker_error:malformed_json_action"
    assert not any(task.origin == "recovery" for task in store.get_all_contracts())
    assert store.get_assets_by_name(f"_fail_context_{env['failed'].id}") == []
    assert store.get_claim(env["failed"].id)["status"] == "released"
    assert any(
        record.record_type == "allowance.extinguished"
        and record.payload.get("contract_id") == env["failed"].id
        and record.payload.get("amount") == 1
        for _revision, record in store.scan_runtime_records()
    )
    store.close()


def test_generic_stage_recovery_with_missing_reserved_name_capability_is_atomic(
    tmp_path,
):
    restricted = (
        "asset.publish",
        "contract.publish",
        "contract.publish.protected",
    )
    env = _world(tmp_path, recovery_capabilities=restricted)
    store = env["store"]
    _failed_claim, failure = _fail_dependency_stage(env)
    assert failure.payload["category"] == "worker_error:malformed_json_action"
    assert not any(task.origin == "recovery" for task in store.get_all_contracts())
    assert store.get_assets_by_name(f"_fail_context_{env['failed'].id}") == []
    rejects = store.scan_runtime_records(record_type="candidate.rejected")
    assert any(
        "asset.publish.protected" in str(record.payload.get("reason", ""))
        for _revision, record in rejects
    )
    assert not any(
        record.record_type == "allowance.reserved"
        and record.payload.get("child_contract_id")
        and store.get_contract(record.payload["child_contract_id"]) is not None
        and store.get_contract(record.payload["child_contract_id"]).origin == "recovery"
        for _revision, record in store.scan_runtime_records()
    )
    store.close()


def test_generic_stage_recovery_authentication_rejection_is_audit_only(tmp_path):
    env = _world(tmp_path, include_recovery_key=False)
    store = env["store"]
    _failed_claim, failure = _fail_dependency_stage(env)
    assert failure.payload["category"] == "worker_error:malformed_json_action"
    assert not any(task.origin == "recovery" for task in store.get_all_contracts())
    assert store.get_assets_by_name(f"_fail_context_{env['failed'].id}") == []
    auth_rejections = store.scan_runtime_records(
        record_type="candidate.authentication_rejected"
    )
    assert auth_rejections
    rejected_candidate_id = auth_rejections[-1][1].payload["claimed_candidate_id"]
    assert not any(
        record.payload.get("candidate_id") == rejected_candidate_id
        for _revision, record in store.scan_runtime_records(
            record_type="candidate.received"
        )
    )
    store.close()


def test_generic_recovery_skips_terminal_funder(tmp_path):
    env = _world(tmp_path)
    store = env["store"]
    empty_publishers = CandidatePublisherRegistry(())
    _failed_claim, failure = _fail_dependency_stage(env, publishers=empty_publishers)
    assert failure.payload["category"] == "worker_error:malformed_json_action"
    assert not any(task.origin == "recovery" for task in store.get_all_contracts())

    # The setup key already owns contract.cancel in this domain, so its signed
    # ordinary cancellation is sufficient to make the funding ancestor terminal.
    cancelled = env["setup"].publish(
        (contract_cancellation_effect(env["root"].id, "terminal funder fixture"),),
        idempotency_key="cancel:noaa-root",
    )
    assert cancelled.accepted

    assert process_worker_failures(store, candidate_publishers=env["publishers"]) == []
    assert any(
        record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == env["root"].id
        for _revision, record in store.scan_runtime_records()
    )
    assert not any(task.origin == "recovery" for task in store.get_all_contracts())
    assert store.get_assets_by_name(f"_fail_context_{env['failed'].id}") == []
    store.close()
