"""Signed method creation, testing, independent acceptance and reuse."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from aigineering.cli._candidate import commit_local_effects, require_accepted
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.local_identity import (
    ensure_local_domain,
    ensure_local_plugin_publisher,
    ensure_local_worker_host,
)
from aigineering.methods.service import MethodService
from aigineering.methods.packages import resolve_package
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.protocol.types import Candidate
from aigineering.runtime import claim_next_package, execute_claimed_package


def package_data(*, version="1.0.0"):
    return {
        "schema": "method-package-v1",
        "name": "check-number",
        "version": version,
        "description": "Check whether a reported count matches the source.",
        "instructions": "Compare source and reported counts; return a boolean matched result.",
        "inputs": ["source"],
        "outputs": {"assessment": {"matched": "boolean"}},
        "requirements": {
            key: []
            for key in (
                "tool_scope",
                "worker_capabilities",
                "worker_pools",
                "delegation_capabilities",
                "delegation_pools",
            )
        },
        "dependencies": [],
        "context_asset_ids": [],
        "examples": [],
    }


def suite_data():
    return {
        "schema": "method-cases-v1",
        "name": "held-out-count-checks",
        "cases": [
            {
                "name": "correct",
                "inputs": {"source": '{"source":3,"reported":3}'},
                "expected": {"assessment": {"matched": True}},
            },
            {
                "name": "incorrect",
                "inputs": {"source": '{"source":3,"reported":4}'},
                "expected": {"assessment": {"matched": False}},
            },
        ],
    }


@pytest.fixture
def domain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SQLiteStore(str(tmp_path / "methods.db"))
    ensure_local_domain(store)
    service = MethodService(
        store, lambda effects, **kw: commit_local_effects(store, effects, **kw)
    )
    yield store, service
    store.close()


def publish_text(store, name, content):
    proposal = build_control_plane_asset(name=name, content=content)
    return require_accepted(
        commit_local_effects(
            store,
            (asset_proposal_effect(proposal),),
            idempotency_key=f"input:{proposal.id}",
        )
    ).assets[0]


class _MethodWorker:
    worker_id = "worker:method-executor"

    def __init__(self, *, wrong=False, generated=None):
        self.wrong = wrong
        self.generated = generated
        self.disclosures = []

    def registration(self):
        return WorkerRegistration(self.worker_id)

    def invoke(self, contract, disclosed_assets):
        self.disclosures.append(tuple(a.id for a in disclosed_assets))
        if self.generated is not None:
            content = json.dumps(self.generated)
        else:
            binding = json.loads(contract.description.splitlines()[-1])
            source = next(
                a for a in disclosed_assets if a.id == binding["inputs"]["source"]
            )
            value = json.loads(source.content)
            content = json.dumps(
                {
                    "matched": True
                    if self.wrong
                    else value["source"] == value["reported"]
                }
            )
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps({"outputs": {contract.outputs[0]: content}}),
        )


def execute(store, task, worker):
    host = ensure_local_worker_host(store, worker)
    claimed = claim_next_package(store, worker_id=host.worker_id, contract_id=task.id)
    assert claimed is not None
    execute_claimed_package(claimed, host, store)


def run_cases(store, run, worker):
    for case in json.loads(run.content)["cases"]:
        execute(store, store.get_contract(case["contract_id"]), worker)


def attest(store, service, evaluation):
    publisher = ensure_local_plugin_publisher(
        store, "method.review.v1", ("asset.attest", "method.verify")
    )
    return require_accepted(
        publisher.publish(
            (service.attestation_effect(evaluation.id),),
            idempotency_key=f"review:{evaluation.id}",
        )
    )


def test_generated_method_is_tested_qualified_and_reused_after_rebuild(
    domain, tmp_path
):
    store, service = domain
    request = publish_text(
        store, "request", "Create a method checking source and reported numeric counts."
    )
    generation = service.create(
        request.id, output="generated_method", name="author-count-check", budget=2
    )
    execute(store, generation, _MethodWorker(generated=package_data()))
    method = next(
        a
        for a in store.get_assets_by_name("generated_method")
        if a.created_by == generation.id
    )
    assert service.inspect(method.id)["status"] == "published"
    source = publish_text(store, "live_source", '{"source":7,"reported":8}')
    with pytest.raises(ValueError, match="qualified evaluation"):
        service.instantiate(
            method.id,
            inputs={"source": source.id},
            outputs={"assessment": "live_assessment"},
            name="reuse",
            budget=1,
        )
    suite = service.import_cases(json.dumps(suite_data()), "heldout")
    run, root = service.prepare_tests(method.id, suite.id, name="checks", budget=1)
    worker = _MethodWorker()
    run_cases(store, run, worker)
    assert all(suite.id not in ids for ids in worker.disclosures)
    evaluation, assessment = service.assess(run.id)
    assert json.loads(evaluation.content)["passed"] is True
    with pytest.raises(ValueError, match="independent qualification"):
        service.instantiate(
            method.id,
            inputs={"source": source.id},
            outputs={"assessment": "live_assessment"},
            name="reuse",
            budget=1,
            evaluation_id=evaluation.id,
        )
    attest(store, service, evaluation)
    task = service.instantiate(
        method.id,
        inputs={"source": source.id},
        outputs={"assessment": "live_assessment"},
        name="reuse",
        budget=1,
        evaluation_id=evaluation.id,
    )
    execute(store, task, _MethodWorker())
    assert json.loads(store.get_assets_by_name("live_assessment")[0].content) == {
        "matched": False
    }
    before = store.runtime_materialization_digest()
    assert store.rebuild_runtime_materializations() == before
    store.close()
    reopened = SQLiteStore(str(tmp_path / "methods.db"))
    reopened_service = MethodService(
        reopened, lambda effects, **kw: commit_local_effects(reopened, effects, **kw)
    )
    assert reopened_service.instantiate(
        method.id,
        inputs={"source": source.id},
        outputs={"assessment": "second_assessment"},
        name="reuse-again",
        budget=1,
        evaluation_id=evaluation.id,
    )
    reopened.close()


def test_failed_method_can_be_revised_without_reusing_old_evaluation(domain):
    store, service = domain
    method = service.import_package(json.dumps(package_data()))
    suite = service.import_cases(json.dumps(suite_data()), "cases")
    run, root = service.prepare_tests(method.id, suite.id, name="bad-checks", budget=1)
    run_cases(store, run, _MethodWorker(wrong=True))
    failed, _ = service.assess(run.id)
    assert json.loads(failed.content)["passed"] is False
    with pytest.raises(ValueError, match="failed evaluation"):
        service.attestation_effect(failed.id)
    request = publish_text(
        store,
        "revise_request",
        "Fix the failed mismatch case; retain the correct case.",
    )
    revision = service.create(
        request.id,
        output="revised_method",
        name="revise",
        budget=2,
        context=(method.id, failed.id),
    )
    revised_data = package_data(version="1.0.1")
    author = _MethodWorker(generated=revised_data)
    execute(store, revision, author)
    assert method.id in author.disclosures[0] and failed.id in author.disclosures[0]
    revised = store.get_assets_by_name("revised_method")[0]
    good_run, _ = service.prepare_tests(
        revised.id, suite.id, name="good-checks", budget=1
    )
    run_cases(store, good_run, _MethodWorker())
    evaluation, _ = service.assess(good_run.id)
    attest(store, service, evaluation)
    source = publish_text(store, "source", '{"source":1,"reported":1}')
    with pytest.raises(ValueError, match="exact package"):
        service.instantiate(
            method.id,
            inputs={"source": source.id},
            outputs={"assessment": "result"},
            name="wrong-version",
            budget=1,
            evaluation_id=evaluation.id,
        )


def test_exact_disclosure_and_tool_grants_fail_closed(domain):
    store, service = domain
    data = package_data()
    data["requirements"]["tool_scope"] = ["count.check"]
    method = service.import_package(json.dumps(data))
    source = publish_text(store, "source", "old")
    publish_text(store, "source", "new")
    with pytest.raises(ValueError, match="explicit invocation grant"):
        service.instantiate(
            method.id,
            inputs={"source": source.id},
            outputs={"assessment": "result"},
            name="no-grant",
            budget=2,
            allow_unverified=True,
        )
    task = service.instantiate(
        method.id,
        inputs={"source": source.id},
        outputs={"assessment": "result"},
        name="granted",
        budget=2,
        allow_unverified=True,
        allowed_tools=("count.check",),
    )
    assert source.id in task.context_asset_ids
    assert len(task.context_asset_ids) == 2
    hidden = replace(method, disclosure_view="redacted")
    with pytest.raises(ValueError, match="disclosure"):
        resolve_package(
            method.id,
            lambda asset_id: (
                hidden if asset_id == method.id else store.get_asset(asset_id)
            ),
        )


def test_prepare_tests_rejects_suite_asset_in_method_context(domain):
    store, service = domain
    suite = service.import_cases(json.dumps(suite_data()), "heldout")
    data = package_data()
    data["context_asset_ids"] = [suite.id]
    method = service.import_package(json.dumps(data))

    with pytest.raises(ValueError, match="test answers must not be in method context"):
        service.prepare_tests(method.id, suite.id, name="leaky-checks", budget=1)


def test_forged_run_and_evaluation_cannot_authorize_reuse(domain):
    from aigineering.methods.evaluation import json_asset

    store, service = domain
    method = service.import_package(json.dumps(package_data()))
    suite = service.import_cases(json.dumps(suite_data()), "cases")
    run, _ = service.prepare_tests(method.id, suite.id, name="authentic", budget=1)
    with pytest.raises(ValueError, match="terminal"):
        service.assess(run.id)
    run_cases(store, run, _MethodWorker())
    evaluation, _ = service.assess(run.id)
    fake = publish_text(store, "copied_assessment", evaluation.content)
    with pytest.raises(ValueError, match="acceptance Contract"):
        service.attestation_effect(fake.id)
    tampered = json.loads(run.content)
    tampered["cases"][0]["contract_id"] = tampered["cases"][1]["contract_id"]
    proposal = json_asset("forged_run", tampered)
    forged = require_accepted(
        commit_local_effects(
            store, (asset_proposal_effect(proposal),), idempotency_key="forged-run"
        )
    ).assets[0]
    with pytest.raises(ValueError, match="immutable test specification"):
        service.assess(forged.id)


def test_evaluation_producer_cannot_self_attest_and_rejection_is_durable(domain):
    from aigineering.core.candidate_publisher import publish_effects

    store, service = domain
    method = service.import_package(json.dumps(package_data()))
    suite = service.import_cases(json.dumps(suite_data()), "cases")
    run, _ = service.prepare_tests(method.id, suite.id, name="self-review", budget=1)
    run_cases(store, run, _MethodWorker())
    evaluation, _ = service.assess(run.id)
    from aigineering.protocol.candidate import ActorKey
    from aigineering.protocol.signing import Ed25519Signer
    from aigineering.protocol.effect_builders import actor_authorization_effect

    signer = Ed25519Signer()
    actor = ActorKey(
        evaluation.signed_by,
        "self-review-key",
        signer.kind,
        signer.signer_id,
        ("asset.attest", "method.verify"),
    )
    require_accepted(
        commit_local_effects(
            store,
            (actor_authorization_effect(actor),),
            idempotency_key="authorize-self-review",
        )
    )
    decision = publish_effects(
        store,
        store,
        ensure_local_domain(store),
        actor,
        signer,
        (service.attestation_effect(evaluation.id),),
        idempotency_key="self-review",
    )
    assert not decision.accepted
    rejections = store.scan_runtime_records(record_type="candidate.rejected")
    assert any(
        "cannot attest its own" in record.payload["reason"] for _, record in rejections
    )
    assert not store.scan_runtime_records(record_type="output.qualified")


def test_dependency_cannot_widen_tools_and_missing_context_fails(domain):
    store, service = domain
    dependency = package_data()
    dependency["requirements"]["tool_scope"] = ["external.write"]
    dep = service.import_package(json.dumps(dependency))
    parent = package_data(version="2.0")
    parent["dependencies"] = [dep.id]
    with pytest.raises(ValueError, match="widens"):
        service.import_package(json.dumps(parent))
    parent["dependencies"] = []
    parent["context_asset_ids"] = ["asset:v1:" + "0" * 64]
    with pytest.raises(ValueError, match="missing exact"):
        service.import_package(json.dumps(parent))


def test_cancelled_test_run_is_failed_evidence_not_success(domain):
    from aigineering.protocol.candidate import CandidateEffect

    store, service = domain
    method = service.import_package(json.dumps(package_data()))
    suite = service.import_cases(json.dumps(suite_data()), "cases")
    run, root = service.prepare_tests(method.id, suite.id, name="cancelled", budget=1)
    require_accepted(
        commit_local_effects(
            store,
            (
                CandidateEffect(
                    "contract.cancel",
                    {"contract_id": root.id, "reason": "stop evaluation"},
                ),
            ),
            idempotency_key="cancel-tests",
        )
    )
    evaluation, _ = service.assess(run.id)
    assert json.loads(evaluation.content)["passed"] is False
    with pytest.raises(ValueError, match="failed evaluation"):
        service.attestation_effect(evaluation.id)


def test_ambiguous_json_output_does_not_pass_exact_evaluation(domain):
    class DuplicateKeyWorker(_MethodWorker):
        def invoke(self, contract, disclosed_assets):
            result = super().invoke(contract, disclosed_assets)
            action = json.loads(result.raw_output.removeprefix("/exec "))
            text = action["outputs"][contract.outputs[0]]
            action["outputs"][contract.outputs[0]] = text.replace(
                "{", '{"matched":false,', 1
            )
            return Candidate(
                worker_id=self.worker_id, raw_output="/exec " + json.dumps(action)
            )

    store, service = domain
    method = service.import_package(json.dumps(package_data()))
    suite = service.import_cases(json.dumps(suite_data()), "cases")
    run, _ = service.prepare_tests(method.id, suite.id, name="duplicate-json", budget=1)
    run_cases(store, run, DuplicateKeyWorker())
    evaluation, _ = service.assess(run.id)
    assert not json.loads(evaluation.content)["passed"]
