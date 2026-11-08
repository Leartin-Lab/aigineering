"""Engine-as-Worker isolation and outer-boundary conformance."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import ModuleType
from conftest import candidate_runtime, hosted_worker
import pytest

from aigineering.agent.engine_worker import EngineWorker
from aigineering.agent.mock import MockWorker
from aigineering.agent.worker import WorkerHost
from aigineering.fleet_config import load_fleet_config
from aigineering.local_identity import ensure_local_domain, ensure_local_worker_host
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.runtime import submit_worker_proposal
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_publication import commit_local_effect
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.run_config import prepare_fleet_resources
from aigineering.protocol.candidate import CandidateClaimBinding
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Asset, Contract


class _OutputWorker:
    """Deterministic fixture that follows the Contract's declared output slots."""

    worker_id = "output_worker"

    def __init__(self, prefix="output"):
        self.prefix = prefix

    def invoke(self, contract, disclosed_assets):
        from aigineering.protocol.types import Candidate

        del disclosed_assets
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps(
                {
                    "outputs": {
                        name: f"{self.prefix}:{contract.id}"
                        for name in contract.outputs
                    }
                }
            ),
        )


class _ShapedOutputWorker:
    def __init__(self, worker_id):
        self.worker_id = worker_id

    def invoke(self, contract, disclosed_assets):
        from aigineering.protocol.types import Candidate

        del disclosed_assets
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps(
                {
                    "outputs": {
                        name: json.dumps({"value": f"service:{contract.id}:{name}"})
                        for name in contract.outputs
                    }
                }
            ),
        )


def test_engine_worker_exports_only_declared_candidate_outputs():
    store = SQLiteStore(":memory:")
    ingress = candidate_runtime(store)
    evidence = ingress.accept_asset(
        Asset(id="asset:evidence", name="evidence", content="source material"),
        source="test",
    )
    contract = Contract(
        id="task:outer",
        name="nested_task",
        inputs=("evidence",),
        outputs=("report",),
        activation="evidence",
        budget=2,
    )
    contract = ingress.accept_contract(contract)
    delegate = _OutputWorker("isolated result")
    worker = EngineWorker(delegate)
    host = hosted_worker(
        store,
        worker,
        genesis=ingress.genesis,
        authority_key=ingress.actor_key,
        authority_signer=ingress.signer,
    )
    preview = worker.invoke(contract, [evidence])

    assert preview.parsed_action == {
        "type": "exec",
        "outputs": {"report": preview.parsed_action["outputs"]["report"]},
    }
    assert "trace" not in preview.raw_output
    assert "asset:evidence" not in preview.raw_output

    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract.id,
    )
    assert claimed is not None
    result = execute_claimed_package(claimed, host, store)

    assert not result["rejected"], result["rejected"]
    assert result["status"] == "accepted", result
    committed = store.get_assets_by_name("report")
    assert len(committed) == 1
    assert committed[0].content.startswith("isolated result:")
    assert committed[0].created_by == contract.id
    assert json.loads(preview.raw_output)["outputs"] == dict(
        preview.parsed_action["outputs"]
    )
    store.close()


def test_default_engine_worker_uses_unique_domain_per_actual_invocation(monkeypatch):
    import aigineering.agent.engine_worker as engine_worker_module

    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    evidence = runtime.accept_asset(
        Asset(id="fresh-domain:evidence", name="evidence", content="input")
    )
    contract = runtime.accept_contract(
        Contract(
            id="fresh-domain:outer",
            name="fresh_domain_task",
            inputs=("evidence",),
            outputs=("report",),
            activation="evidence",
            budget=2,
        )
    )
    domains = []
    initialize = engine_worker_module._initialize_inner_domain

    def capture_domain(inner, worker_id, signer, *, domain_scope=None):
        result = initialize(inner, worker_id, signer, domain_scope=domain_scope)
        domains.append(result[1].domain)
        return result

    monkeypatch.setattr(
        engine_worker_module, "_initialize_inner_domain", capture_domain
    )
    worker = EngineWorker(
        _OutputWorker("isolated result"), worker_id="worker:engine-fresh-domain"
    )
    host = hosted_worker(
        store,
        worker,
        genesis=runtime.genesis,
        authority_key=runtime.actor_key,
        authority_signer=runtime.signer,
    )
    preview = worker.invoke(contract, [evidence])
    claimed = claim_next_package(
        store, worker_id=host.worker_id, contract_id=contract.id
    )
    assert claimed is not None
    binding = CandidateClaimBinding(
        contract_id=contract.id,
        claim_id=claimed.package.claim_id,
        claim_epoch=claimed.package.claim_epoch,
        package_id=claimed.package.package_id,
    )
    first = worker.invoke_claimed(contract, [evidence], binding)
    second = worker.invoke_claimed(contract, [evidence], binding)
    assert first.parsed_action["outputs"]["report"].startswith("isolated result:")
    assert second.parsed_action != first.parsed_action
    assert preview.parsed_action["outputs"]["report"].startswith("isolated result:")
    assert preview.parsed_action != first.parsed_action
    assert len(domains) == 3
    assert len(set(domains)) == 3

    result = execute_claimed_package(claimed, host, store)
    assert result["status"] == "accepted", result
    assert len(domains) == 4
    assert len(set(domains)) == 4
    assert store.get_assets_by_name("report")[0].content.startswith("isolated result:")
    store.close()


def test_engine_worker_unfinished_inner_run_fails_visibly_at_outer_boundary():
    store = SQLiteStore(":memory:")
    contract = Contract(
        id="task:outer-failure", name="nested", outputs=("report",), budget=1
    )
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(contract)
    worker = EngineWorker(MockWorker(), max_steps=1)
    host = hosted_worker(
        store,
        worker,
        genesis=runtime.genesis,
        authority_key=runtime.actor_key,
        authority_signer=runtime.signer,
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract.id,
    )
    assert claimed is not None

    result = execute_claimed_package(claimed, host, store)

    assert result["status"] == "rejected"
    assert store.get_assets_by_name("report") == []
    terminal = store.scan_runtime_records(record_type="lifecycle.terminal")
    assert terminal[-1][1].payload["terminal"] == "failed"
    store.close()


def test_engine_worker_restarts_from_inner_facts_and_fences_late_outer_result(
    tmp_path,
):
    outer = SQLiteStore(":memory:")
    runtime = candidate_runtime(outer)
    evidence = runtime.accept_asset(
        Asset(id="bridge:evidence", name="evidence", content="durable input")
    )
    root = runtime.accept_contract(
        Contract(
            id="bridge:root",
            name="durable_bridge",
            inputs=("evidence",),
            outputs=("report",),
            activation="evidence",
            budget=2,
        )
    )
    inner_path = str(tmp_path / "engine-worker-inner.db")
    inner_private_key = Ed25519Signer().private_key_hex
    first = EngineWorker(
        _OutputWorker("inner result"),
        inner_store_factory=lambda: SQLiteStore(inner_path),
        inner_signer=Ed25519Signer.from_private_key_hex(inner_private_key),
    )
    host = hosted_worker(
        outer,
        first,
        genesis=runtime.genesis,
        authority_key=runtime.actor_key,
        authority_signer=runtime.signer,
    )
    claimed = claim_next_package(outer, worker_id=host.worker_id, contract_id=root.id)
    assert claimed is not None
    binding = CandidateClaimBinding(
        contract_id=root.id,
        claim_id=claimed.package.claim_id,
        claim_epoch=claimed.package.claim_epoch,
        package_id=claimed.package.package_id,
    )
    inner_result = first.invoke_claimed(root, [evidence], binding)
    envelope = CandidateEnvelope(
        contract_id=root.id,
        worker_id=host.worker_id,
        raw_output=inner_result.raw_output,
        parsed_action=inner_result.parsed_action,
        claim_id=binding.claim_id,
        claim_epoch=binding.claim_epoch,
        package_id=binding.package_id,
        idempotency_key=f"run-{binding.package_id}",
    )
    stale = host.sign_envelope(envelope, contract=root)
    outer.mark_claim_released(binding.claim_id)

    with pytest.raises(ValueError, match="active worker claim predicate failed"):
        submit_worker_proposal(stale, outer)
    assert outer.get_assets_by_name("report") == []

    class _MustNotRun:
        worker_id = "mock_worker"

        def invoke(self, contract, disclosed_assets):
            raise AssertionError("accepted inner facts should be reused after restart")

    restarted = EngineWorker(
        _MustNotRun(),
        inner_store_factory=lambda: SQLiteStore(inner_path),
        inner_signer=Ed25519Signer.from_private_key_hex(inner_private_key),
    )
    replayed = restarted.invoke_claimed(root, [evidence], binding)
    assert replayed.parsed_action == inner_result.parsed_action
    assert replayed.raw_output == inner_result.raw_output

    replacement = runtime.accept_contract(
        Contract(
            id="bridge:replacement",
            parent_id=root.id,
            name=root.name,
            description=root.description,
            inputs=root.inputs,
            outputs=root.outputs,
            activation=root.activation,
            budget=2,
        )
    )
    next_worker = EngineWorker(
        _MustNotRun(),
        inner_store_factory=lambda: SQLiteStore(inner_path),
        inner_signer=Ed25519Signer.from_private_key_hex(inner_private_key),
    )
    next_host = WorkerHost(next_worker, host.genesis, host.actor_key, host.signer)
    next_claim = claim_next_package(
        outer, worker_id=next_host.worker_id, contract_id=replacement.id
    )
    assert next_claim is not None
    result = execute_claimed_package(next_claim, next_host, outer)
    assert result["status"] == "accepted", result
    assert outer.get_assets_by_name("report")[-1].content.startswith("inner result:")
    inner = SQLiteStore(inner_path)
    operations = [
        asset
        for asset in inner.get_all_assets()
        if asset.name.startswith("bridge_operation:")
    ]
    assert len(operations) == 2
    completions = [
        json.loads(asset.content)
        for asset in inner.get_all_assets()
        if asset.name.startswith("bridge_completion:")
    ]
    assert len(completions) == 2
    assert len({item["inner_contract_id"] for item in completions}) == 2
    assert len(inner.get_assets_by_name("report")) == 1
    inner.close()
    outer.close()


def test_existing_workspace_engine_worker_uses_pinned_receiver_fleet(
    tmp_path, monkeypatch
):
    module = ModuleType("engine_receiver_test")
    module.build_worker = lambda *, worker_id: _ShapedOutputWorker(worker_id)
    module.tool_version = "1.0"

    def build_registry():
        from aigineering.core.tools import ToolRegistry
        from aigineering.protocol.types import ToolSpec

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="receiver_lookup",
                description="receiver lookup",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "string"},
                version=module.tool_version,
            ),
            lambda _arguments: '"ok"',
        )
        return registry

    module.build_registry = build_registry
    monkeypatch.setitem(sys.modules, "engine_receiver_test", module)
    source_workspace = tmp_path / "workspaces" / "A"
    receiver_workspace = tmp_path / "workspaces" / "B"
    config_dir = tmp_path / "config-outside-workspaces"
    source_identity = source_workspace / ".aig" / "identity" / "root.ed25519"
    receiver_identity = receiver_workspace / ".aig" / "identity" / "root.ed25519"
    source_db = source_workspace / ".aig" / "store.db"
    receiver_db = receiver_workspace / ".aig" / "store.db"
    source_workspace.mkdir(parents=True)
    receiver_workspace.mkdir(parents=True)
    config_dir.mkdir()

    monkeypatch.setattr("aigineering.application.require_redis", lambda *_: None)
    monkeypatch.setattr(
        "aigineering.runtime_services.sync_runtime",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_args, **_kwargs: None
    )

    with SQLiteStore(str(source_db)) as source_store:
        source_genesis = ensure_local_domain(source_store, key_path=source_identity)
        from aigineering.protocol.types import Asset

        input_decision = commit_local_effect(
            source_store,
            asset_proposal_effect(
                Asset(id="input", name="evidence", content="source evidence")
            ),
            idempotency_key="test-input-publication",
            key_path=source_identity,
        )
        evidence = input_decision.assets[0]
    with SQLiteStore(str(receiver_db)) as receiver_store:
        receiver_genesis = ensure_local_domain(
            receiver_store, key_path=receiver_identity
        )
        preexisting_same_content = commit_local_effect(
            receiver_store,
            asset_proposal_effect(
                Asset(
                    id="receiver-existing-evidence",
                    name=evidence.name,
                    content=evidence.content,
                    origin="receiver-local",
                    trust_tier="trusted",
                    promptable=False,
                )
            ),
            idempotency_key="receiver-existing-same-content",
            key_path=receiver_identity,
        ).assets[0]

    receiver_config = config_dir / "receiver.toml"
    receiver_fleet_text = (
        "[fleet]\n"
        'db_path = ".aig/store.db"\n\n'
        "[[workers]]\n"
        'id = "receiver-worker"\n'
        'kind = "local"\n'
        'worker_factory = "engine_receiver_test:build_worker"\n'
        "\n"
        "[[workers]]\n"
        'id = "receiver-tools"\n'
        'kind = "tool"\n'
        'tool_registry = "engine_receiver_test:build_registry"\n'
    )
    receiver_config.write_text(receiver_fleet_text, encoding="utf-8")
    source_config = config_dir / "source.toml"
    source_config.write_text(
        "[fleet]\n"
        'db_path = ".aig/store.db"\n\n'
        "[[workers]]\n"
        'id = "engine-bridge"\n'
        'kind = "engine"\n'
        'target_workspace = "../B"\n'
        f'target_domain_id = "{receiver_genesis.id}"\n'
        'receiver_fleet = "receiver.toml"\n'
        'receiver_key_file = ".aig/identity/root.ed25519"\n',
        encoding="utf-8",
    )

    fleet = load_fleet_config(source_config)
    resources = prepare_fleet_resources(
        fleet, base_dir=config_dir, workspace_dir=source_workspace
    )
    engine = resources.workers[0][1]
    assert isinstance(engine, EngineWorker)
    assert engine.expected_domain_id == source_genesis.id

    with SQLiteStore(str(source_db)) as source_store:
        source_host = ensure_local_worker_host(
            source_store, engine, key_path=source_identity
        )
        outer = Contract(
            id="source:outer",
            name="existing_workspace_report",
            inputs=("evidence",),
            outputs=("report",),
            budget=3,
            labels=("evidence",),
            context_asset_ids=(evidence.id,),
            acceptance_policy={
                "mode": "independent",
                "policy_version": "test-policy-v1",
                "verifier_capabilities": ["review"],
                "rubric_asset_ids": ["source-rubric-id"],
                "evidence_asset_ids": ["source-evidence-id"],
                "output_shapes": {"report": {"value": "string"}},
            },
        )
        binding = CandidateClaimBinding(
            contract_id=outer.id,
            claim_id="claim:existing-workspace-test",
            claim_epoch=1,
            package_id="pkg:existing-workspace-test",
        )
        candidate = source_host.invoke(outer, [evidence], claim_binding=binding)
        if candidate.parsed_action is None:
            with SQLiteStore(str(receiver_db)) as debug_store:
                rejections = debug_store.scan_runtime_records(
                    record_type="candidate.rejected"
                )
                pytest.fail(
                    "Engine bridge rejected before output: "
                    + str(
                        rejections[-1][1].payload
                        if rejections
                        else candidate.raw_output
                    )
                )
        assert candidate.parsed_action is not None
        assert json.loads(candidate.parsed_action["outputs"]["report"])[
            "value"
        ].startswith("service:")
        replay = source_host.invoke(outer, [evidence], claim_binding=binding)
        assert replay.parsed_action == candidate.parsed_action

        module.tool_version = "2.0"
        changed_resources = prepare_fleet_resources(
            fleet, base_dir=config_dir, workspace_dir=source_workspace
        )
        changed_engine = changed_resources.workers[0][1]
        changed_candidate = changed_engine.invoke_claimed(outer, [evidence], binding)
        assert changed_candidate.parsed_action is None
        assert "bridge operation mapping" in changed_candidate.raw_output
        module.tool_version = "1.0"
        assert (
            source_host.invoke(outer, [evidence], claim_binding=binding).parsed_action
            == candidate.parsed_action
        )
        with pytest.raises(ValueError, match="expected domain"):
            WorkerHost(
                engine, receiver_genesis, source_host.actor_key, source_host.signer
            )
        assert source_store.get_assets_by_name("report") == []

    with SQLiteStore(str(receiver_db)) as receiver_store:
        assert (
            ensure_local_domain(receiver_store, key_path=receiver_identity).id
            == receiver_genesis.id
        )
        report_assets = receiver_store.get_assets_by_name("report")
        assert len(report_assets) == 1
        assertion = next(
            item
            for item in receiver_store.get_definition_content_assertions(
                definition_id=report_assets[0].definition_hash,
                content_id=report_assets[0].content_hash,
            )
            if item["actor_id"] == report_assets[0].signed_by
        )
        inner_root = receiver_store.get_contract(assertion["evidence"]["contract_id"])
        assert inner_root is not None
        from aigineering.core.input_bindings import input_asset_bindings

        mapped_ids = input_asset_bindings(inner_root.sensitive_input_policy)["evidence"]
        assert len(mapped_ids) == 1
        assert mapped_ids[0] != evidence.id
        assert mapped_ids[0] != preexisting_same_content.id
        assert receiver_store.get_asset(mapped_ids[0]).content == evidence.content
        assert (
            len(
                [
                    asset
                    for asset in receiver_store.get_assets_by_name(evidence.name)
                    if asset.content == evidence.content
                ]
            )
            == 2
        )
        assert inner_root.context_asset_ids == mapped_ids
        assert inner_root.acceptance_policy["mode"] == "mechanical"
        assert "output_source" not in inner_root.acceptance_policy
        assert inner_root.acceptance_policy["output_requirements"] == {
            "report": {"definition_id": report_assets[0].definition_hash}
        }
        assert inner_root.acceptance_policy["output_shapes"] == {
            "report": {"value": "string"}
        }
        assert "rubric_asset_ids" not in inner_root.acceptance_policy
        bridge_ops = [
            asset
            for asset in receiver_store.get_all_assets()
            if asset.name.startswith("bridge_operation:")
        ]
        assert len(bridge_ops) == 1
        operation = json.loads(bridge_ops[0].content)
        assert operation["source_domain_id"] == source_genesis.id
        assert operation["target_domain_id"] == receiver_genesis.id
        assert operation["outer_claim"]["claim_id"] == binding.claim_id
        assert operation["outer_claim"]["package_id"] == binding.package_id
    assert Path(source_workspace / ".aig" / "store.db").is_file()
