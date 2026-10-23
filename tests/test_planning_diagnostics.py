"""Compile diagnostics survive the signed failure and recovery boundaries."""

from __future__ import annotations

import json

import pytest
from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.agent.worker import WorkerExecutionError
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.signing import Ed25519Signer
from aigineering.planning_diagnostics import (
    MAX_COMPILE_DIAGNOSTIC_BYTES,
    MAX_COMPILE_FIELD_BYTES,
    build_compile_diagnostics,
    valid_compile_diagnostics,
)
from aigineering.plugins.planning import (
    PlanningCompileError,
    compile_planning_blueprint,
)
from aigineering.protocol.immutability import deep_freeze, deep_thaw
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.candidate import ActorKey
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
    process_worker_failures,
)
from aigineering.task_diagnostics import (
    _invocation_failure_matches_grant,
    task_diagnostics,
    task_invocation_failure_binding,
    task_runs,
)
from aigineering.task_lineage import task_lineage


def _blueprint():
    return json.dumps(
        {
            "contracts": [
                {
                    "name": "lookup",
                    "description": "Use a permitted tool.",
                    "inputs": [],
                    "outputs": ["report"],
                    "budget": 1,
                    "tool_scope": ["search"],
                    "capability_needs": ["tool_calling"],
                }
            ]
        }
    )


def _compile_contract(label="plugin:plan.compile", parent_id=None):
    return contract_from_fields(
        name="compile",
        description='{"allowed_inputs":[]}',
        inputs=(),
        activation="",
        origin="plugin",
        outputs=("report",),
        labels=(label,),
        budget=4,
        tool_scope=("search",),
        delegation_capabilities=("tool_calling",),
        parent_id=parent_id,
    )


def test_compiler_and_worker_errors_keep_frozen_details_and_existing_summary():
    with pytest.raises(PlanningCompileError) as raised:
        compile_planning_blueprint(
            _compile_contract(), {"planning_blueprint": _blueprint()}, allowance=4
        )
    error = raised.value
    assert error.fields == ("budget", "output_recommitment")
    rejection = error.rejections[0]
    assert rejection["child_name"] == "lookup"
    assert rejection["expected"] == "integer >= 2 when tool_scope is non-empty"
    assert rejection["actual"] == "1"
    assert rejection["recoverable"] is True
    with pytest.raises(TypeError):
        rejection["actual"] = "changed"
    worker_error = WorkerExecutionError(
        "fixture",
        str(error),
        compile_rejections=error.rejections,
        usage_metadata={"prompt_tokens": 2},
    )
    assert worker_error.compile_rejections == error.rejections
    assert worker_error.usage_metadata == {"prompt_tokens": 2}
    assert str(worker_error) == str(error)
    assert PlanningCompileError("early").rejections == ()
    assert WorkerExecutionError("other").compile_rejections == ()


@pytest.mark.parametrize("count", [1, 16, 17])
def test_bounded_details_preserve_prefix_order_and_missing_fields(count):
    source = tuple(
        {"field": str(index), "reason": "fix this"} for index in range(count)
    )
    details = build_compile_diagnostics(deep_freeze(source))
    assert valid_compile_diagnostics(deep_freeze(details))
    assert details["original_count"] == count
    assert details["omitted_count"] == max(0, count - 16)
    assert [entry["values"] for entry in details["entries"]] == list(source[:16])
    assert build_compile_diagnostics(source) == details
    assert "recoverable" not in details["entries"][0]["values"]


@pytest.mark.parametrize("text", ["a", "证", "😀", '\\"\n'])
def test_field_and_total_json_byte_limits_are_explicit(text):
    source = tuple(
        {
            key: text * 1200
            for key in ("child_name", "field", "reason", "action", "expected", "actual")
        }
        for _ in range(20)
    )
    details = build_compile_diagnostics(source)
    encoded = json.dumps(
        details, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )
    assert len(encoded.encode("ascii")) <= MAX_COMPILE_DIAGNOSTIC_BYTES
    assert details["omitted_count"] == 20 - len(details["entries"])
    assert details["omitted_count"] > 0
    assert valid_compile_diagnostics(deep_freeze(details))
    for entry in details["entries"]:
        assert entry["truncated_fields"] == sorted(source[0])
        for key, value in entry["values"].items():
            assert source[0][key].startswith(value)
            assert len(json.dumps(value, ensure_ascii=True)) <= MAX_COMPILE_FIELD_BYTES


@pytest.mark.parametrize(
    "source",
    [(), ({"unknown": "body"},), ({"recoverable": 1},), ({"reason": "\ud800"},)],
)
def test_invalid_optional_adapter_details_keep_textual_fallback(source):
    assert build_compile_diagnostics(source) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(version=True),
        lambda value: value.update(version=2),
        lambda value: value.update(original_count=False),
        lambda value: value.update(omitted_count=9),
        lambda value: value.update(entries="bad"),
        lambda value: value.update(entries=[]),
        lambda value: value.update(extra="bad"),
        lambda value: value["entries"][0]["values"].update(recoverable=1),
        lambda value: value["entries"][0]["values"].update(reason="x" * 600),
        lambda value: value["entries"][0].update(truncated_fields=[False]),
    ],
)
def test_durable_details_reject_malformed_containers_and_values(mutate):
    value = build_compile_diagnostics(({"reason": "fix this"},))
    mutate(value)
    assert not valid_compile_diagnostics(deep_freeze(value))


@pytest.mark.parametrize("label", ["plugin:plan.compile", "plugin:replan.compile"])
@pytest.mark.parametrize("crash_after_publication", [False, True])
@pytest.mark.parametrize("oversized", [False, True])
def test_compile_failure_reopens_and_discloses_exact_details_without_republishing(
    tmp_path,
    monkeypatch,
    label,
    crash_after_publication,
    oversized,
):
    path = tmp_path / "compile.sqlite"
    store = SQLiteStore(path)
    setup = candidate_runtime(store)
    recovery_signer = Ed25519Signer()
    recovery_key = ActorKey(
        "plugin:recovery.publish.v1",
        "recovery-key",
        recovery_signer.kind,
        recovery_signer.signer_id,
        (
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
        ),
    )
    setup.authorize_actor(recovery_key)
    parent = setup.accept_contract(
        contract_from_fields(
            name="parent",
            description="Parent obligation",
            inputs=(),
            activation="",
            origin="human",
            labels=(),
            outputs=("root_report",),
            budget=20,
            tool_scope=("search",),
            delegation_capabilities=("tool_calling",),
        )
    )
    failed = setup.accept_contract(_compile_contract(label, parent_id=parent.id))
    blueprint = _blueprint()
    if oversized:
        child = json.loads(blueprint)["contracts"][0]
        blueprint = json.dumps(
            {
                "contracts": [
                    dict(child, name=f"{index}-" + "证" * 800) for index in range(20)
                ]
            }
        )
    raw = "/exec " + json.dumps({"outputs": {"planning_blueprint": blueprint}})
    host = hosted_worker(
        store,
        MockWorker({failed.name: raw}, worker_id="worker:compile"),
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=failed.id,
        claim_candidate=host.claim_candidate(
            request_id="compile-claim", contract_id=failed.id
        ),
        expected_registration_version=host.registration_version,
    )
    assert claimed is not None
    publishers = CandidatePublisherRegistry(
        (
            (
                "recovery.publish.v1",
                CandidatePublisher(
                    store,
                    store,
                    setup.genesis,
                    recovery_key,
                    recovery_signer,
                ),
            ),
        )
    )
    import aigineering.runtime as runtime_module

    original_commit = runtime_module._commit_recovery_outcome
    if crash_after_publication:

        def crash(*args, **kwargs):
            raise RuntimeError("fixture interruption after publication")

        monkeypatch.setattr(runtime_module, "_commit_recovery_outcome", crash)
    with pytest.raises(
        RuntimeError if crash_after_publication else WorkerInvocationError
    ):
        execute_claimed_package(claimed, host, store, candidate_publishers=publishers)
    monkeypatch.setattr(runtime_module, "_commit_recovery_outcome", original_commit)
    failure = store.scan_runtime_records(record_type="worker.invocation_failed")[0][1]
    assert (
        failure.payload["category"]
        == "worker_error:planning_compile_rejected:budget,output_recommitment"
    )
    details = deep_thaw(failure.payload["compile_diagnostics"])
    assert details["entries"][0]["values"]["actual"] == "1"
    assert details["entries"][0]["values"]["recoverable"] is True
    if oversized:
        assert details["original_count"] == 21
        assert details["omitted_count"] > 0
        assert "child_name" in details["entries"][0]["truncated_fields"]
    assert store.get_claim(failed.id)["status"] == "released"
    contracts_before = tuple(store.get_all_contracts())
    receipts_before = len(store.scan_runtime_records(record_type="candidate.received"))
    store.close()

    reopened = SQLiteStore(path)
    publishers = CandidatePublisherRegistry(
        (
            (
                "recovery.publish.v1",
                CandidatePublisher(
                    reopened,
                    reopened,
                    setup.genesis,
                    recovery_key,
                    recovery_signer,
                ),
            ),
        )
    )
    assert process_worker_failures(reopened, candidate_publishers=publishers) == (
        [failed.id] if crash_after_publication else []
    )
    assert process_worker_failures(reopened, candidate_publishers=publishers) == []
    assert tuple(reopened.get_all_contracts()) == contracts_before
    assert (
        len(reopened.scan_runtime_records(record_type="candidate.received"))
        == receipts_before
    )
    assert task_runs(reopened, failed.id)["ok"]
    assert task_diagnostics(reopened, failed.id, claimed.package.claim_id)["ok"]
    binding = task_invocation_failure_binding(
        reopened,
        failed.id,
        claimed.package.claim_id,
        claimed.package.package_id,
        host.worker_id,
        claimed.package.claim_epoch,
    )
    assert binding["failure_record_id"] == failure.id
    lineage = task_lineage(reopened, failed.id)
    assert lineage["ok"]
    assert any(
        edge["kind"] == "recovery_publication" for edge in lineage["data"]["edges"]
    )
    recovery = next(
        contract
        for contract in reopened.get_all_contracts()
        if contract.origin == "recovery"
    )
    context = reopened.get_assets_by_name(f"_fail_context_{failed.id}")[0]
    summary = json.loads(context.content)["rejections"][0]
    assert summary["compile_diagnostics"] == details
    assert summary["category"] == failure.payload["category"]
    assert summary["reject_reason"] == failure.payload["diagnostic"]
    recovery_claim = claim_next_package(
        reopened,
        worker_id=host.worker_id,
        contract_id=recovery.id,
        claim_candidate=host.claim_candidate(
            request_id="recovery-claim", contract_id=recovery.id
        ),
        expected_registration_version=host.registration_version,
    )
    assert recovery_claim is not None
    assert any(
        asset.id == context.id and json.loads(asset.content)["rejections"][0] == summary
        for asset in recovery_claim.disclosed_assets
    )
    assert recovery.parent_id == failed.parent_id
    assert recovery.outputs == failed.outputs
    assert recovery.tool_scope == failed.tool_scope
    assert recovery.labels == failed.labels
    assert recovery.budget == failed.budget
    assert recovery.acceptance_policy == failed.acceptance_policy
    terminals = [
        record
        for _, record in reopened.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("contract_id") == failed.id
    ]
    assert len(terminals) == 1
    assert terminals[0].payload["terminal"] == "failed"
    assert terminals[0].causal_parents == (failure.id,)
    reopened.close()


def test_optional_budget_counts_and_missing_values_are_preserved():
    source = (
        {"requested": 4, "effective": 2, "remaining": None, "reason": "contained"},
    )
    details = build_compile_diagnostics(source)
    assert details["entries"][0]["values"] == source[0]
    assert valid_compile_diagnostics(deep_freeze(details))


@pytest.mark.parametrize("length", [509, 510, 511])
def test_field_limit_counts_json_quotes(length):
    details = build_compile_diagnostics(({"reason": "a" * length},))
    entry = details["entries"][0]
    assert len(entry["values"]["reason"]) == min(length, 510)
    assert entry["truncated_fields"] == (["reason"] if length > 510 else [])


@pytest.mark.parametrize("details", [None, [], {"version": True}])
def test_claim_proof_refuses_malformed_new_field_after_valid_base_record(details):
    grant = {
        "claim_id": "claim:fixture",
        "contract_id": "task:fixture",
        "package_id": "pkg:fixture",
        "worker_id": "worker:fixture",
    }
    payload = dict(
        grant,
        category="worker_error:planning_compile_rejected:budget",
        retryable=False,
        status_code=0,
    )
    valid = build_compile_diagnostics(({"field": "budget", "actual": "1"},))
    assert _invocation_failure_matches_grant(
        create_runtime_record(
            "worker.invocation_failed", dict(payload, compile_diagnostics=valid)
        ),
        grant,
    )
    assert not _invocation_failure_matches_grant(
        create_runtime_record(
            "worker.invocation_failed", dict(payload, compile_diagnostics=details)
        ),
        grant,
    )
    assert not _invocation_failure_matches_grant(
        create_runtime_record(
            "worker.invocation_failed",
            dict(payload, compile_diagnostics=valid, extra="unknown"),
        ),
        grant,
    )
    assert not _invocation_failure_matches_grant(
        create_runtime_record(
            "worker.invocation_failed",
            dict(payload, category="provider_error", compile_diagnostics=valid),
        ),
        grant,
    )


def test_early_compile_failure_retains_legacy_payload_without_inventing_details(
    tmp_path,
):
    with SQLiteStore(tmp_path / "early.sqlite") as store:
        setup = candidate_runtime(store)
        failed = setup.accept_contract(_compile_contract())
        host = hosted_worker(
            store,
            MockWorker({failed.name: '/exec {"outputs":{"wrong":"value"}}'}),
            genesis=setup.genesis,
            authority_key=setup.actor_key,
            authority_signer=setup.signer,
        )
        claimed = claim_next_package(
            store,
            worker_id=host.worker_id,
            contract_id=failed.id,
            claim_candidate=host.claim_candidate(
                request_id="early-claim", contract_id=failed.id
            ),
            expected_registration_version=host.registration_version,
        )
        assert claimed is not None
        with pytest.raises(WorkerInvocationError):
            execute_claimed_package(claimed, host, store)
        failure = store.scan_runtime_records(record_type="worker.invocation_failed")[0][
            1
        ]
        assert (
            failure.payload["category"]
            == "worker_error:planning_compile_rejected:outputs"
        )
        assert "compile_diagnostics" not in failure.payload
        assert (
            failure.payload["diagnostic"]
            == "planning compile must return exactly one structured blueprint"
        )
        assert task_runs(store, failed.id)["ok"]
