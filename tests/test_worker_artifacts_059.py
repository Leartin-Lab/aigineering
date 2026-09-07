"""Business artifact envelopes emitted through the canonical worker compiler."""

from __future__ import annotations

import json

from conftest import candidate_runtime
from test_artifacts_059 import _publish
from test_harness_adapter import _claim

from aigineering.business.artifacts import (
    attachment,
    document,
    envelope,
    evidence,
    lineage,
    report,
)
from aigineering.core.ids import hash_contract_v3
from aigineering.core.signing import Ed25519Signer
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    worker_registration_effect,
)
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.agent.harness import HarnessCandidateAdapter
from aigineering.protocol.types import Contract
from aigineering.runtime import submit_worker_proposal


def test_harness_exec_commits_a_report_artifact_envelope() -> None:
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.core.candidate_publisher import publish_effect

    store = SQLiteStore(":memory:")
    signer = Ed25519Signer()
    actor = ActorKey(
        "harness:artifact",
        "artifact-1",
        signer.kind,
        signer.signer_id,
        (
            "actor.authorize",
            "asset.publish",
            "asset.publish.protected",
            "asset.relate",
            "contract.publish",
            "worker.register",
            "worker.submit",
        ),
    )
    genesis = create_genesis_manifest(
        "worker-artifacts", (actor,), "policy:worker-artifacts"
    )
    from aigineering.core.domain import initialize_genesis

    initialize_genesis(store, genesis)
    runtime = candidate_runtime(store, genesis=genesis, actor_key=actor, signer=signer)
    publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        worker_registration_effect(
            WorkerRegistration(
                actor.actor_id, actor_id=actor.actor_id, key_id=actor.key_id
            )
        ),
        idempotency_key="worker-artifact-register",
    )
    fields = {
        "name": "worker_artifact_task",
        "description": "Return an artifact report.",
        "inputs": ("citation",),
        "outputs": ("report",),
        "activation": "",
        "budget": 5,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    contract = Contract(id=hash_contract_v3(**fields), **fields)
    publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(contract),
        idempotency_key="worker-artifact-contract",
    )
    adapter = HarnessCandidateAdapter(genesis.id, actor, signer)

    source = _publish(
        runtime,
        attachment("source", b"alpha beta", media_type="text/plain"),
        "worker-artifact-source",
    )
    extracted = _publish(
        runtime,
        document(
            "source-document",
            source,
            {
                "schema": "document-extraction-v1",
                "pages": [{"page": 1, "text": "alpha beta"}],
                "tool": "test",
                "version": "1",
                "ocr": False,
            },
        ),
        "worker-artifact-document",
    )
    cited = _publish(
        runtime,
        evidence("citation", extracted, page=1, start=0, end=5),
        "worker-artifact-evidence",
    )

    claimed = _claim(store, contract, adapter)
    report_proposal = report("worker-report", "Finding[^one]", {"one": cited.id})
    proposal = adapter.result_candidate(
        claimed.package,
        "/exec "
        + json.dumps(
            {"outputs": {contract.outputs[0]: report_proposal.content}},
            ensure_ascii=False,
        ),
    )
    assert submit_worker_proposal(proposal, store)["status"] == "accepted"

    output = store.get_assets_by_name(contract.outputs[0])[-1]
    # The canonical Worker graph compiler currently labels all /exec content
    # as text; the business adapter validates the versioned envelope itself.
    assert output.content_type == "text"
    assert envelope(output) == envelope(report_proposal)
    assert lineage(output.id, store.get_asset)["root"] == output.id
