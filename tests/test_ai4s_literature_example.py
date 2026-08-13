"""Executable evidence that the AI4S literature example crosses real boundaries."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from aigineering.agent.harness import HarnessCandidateAdapter
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_v3
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_coordination import authenticate_worker_command
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    worker_registration_effect,
)
from aigineering.protocol.types import Contract
from aigineering.runtime import claim_next_package, submit_worker_proposal

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "examples" / "literature-evidence"
SCRIPT = EXAMPLE / "scripts" / "openalex_search.py"
FIXTURE = EXAMPLE / "assets" / "openalex-response.json"


def _module():
    spec = importlib.util.spec_from_file_location("openalex_search", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _action(*, max_records: int = 2) -> str:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--query",
            "retrieval augmented generation",
            "--fixture",
            str(FIXTURE),
            "--max-records",
            str(max_records),
            "--from-year",
            "2020",
            "--to-year",
            "2026",
            "--retrieved-at",
            "2026-08-11T00:00:00+00:00",
            "--action",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_openalex_adapter_emits_bounded_replayable_action():
    action = _action()
    outer = json.loads(action.removeprefix("/exec "))
    manifest = json.loads(outer["outputs"]["retrieval_manifest"])

    assert manifest["schema_version"] == "literature-retrieval-v1"
    assert manifest["source_count"] == 3
    assert manifest["returned_count"] == 2
    assert manifest["truncated"] is True
    assert manifest["filters"] == {
        "from_publication_date": "2020-01-01",
        "to_publication_date": "2026-12-31",
    }
    assert manifest["records"][0]["id"] == "https://openalex.org/W0000000001"
    assert "api_key" not in action.lower()


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"meta": {}, "results": []}, "meta.count"),
        ({"meta": {"count": 1}, "results": ["not-an-object"]}, "entries"),
        (
            {"meta": {"count": 1}, "results": [{"id": "W1"}]},
            "missing a title",
        ),
    ],
)
def test_openalex_adapter_rejects_success_shaped_bad_data(payload, message):
    module = _module()
    with pytest.raises(module.RetrievalError, match=message):
        module.normalize_response(
            payload,
            query="q",
            max_records=10,
            retrieved_at="2026-08-11T00:00:00+00:00",
        )


def test_retrieval_action_commits_and_rebuilds_through_harness(tmp_path: Path):
    db_path = str(tmp_path / "literature.db")
    store = SQLiteStore(db_path)
    signer = Ed25519Signer()
    actor = ActorKey(
        "harness:literature",
        "literature-1",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "worker.register", "worker.submit"),
    )
    genesis = create_genesis_manifest(
        "literature-example", (actor,), "policy:literature-example"
    )
    initialize_genesis(store, genesis)
    publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        worker_registration_effect(
            WorkerRegistration(
                actor.actor_id,
                profile_id="literature-retrieval-v1",
                actor_id=actor.actor_id,
                key_id=actor.key_id,
            )
        ),
        idempotency_key="register-literature-worker",
    )
    fields = {
        "name": "literature_retrieve",
        "description": "Retrieve a bounded, reproducible literature manifest.",
        "inputs": (),
        "outputs": ("retrieval_manifest",),
        "activation": "",
        "budget": 2,
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
        idempotency_key="publish-literature-task",
    )
    adapter = HarnessCandidateAdapter(genesis.id, actor, signer)
    claim = authenticate_worker_command(
        adapter.claim_candidate(request_id="claim-literature", contract_id=contract.id),
        "worker.claim",
        store,
    )
    claimed = claim_next_package(
        store,
        worker_id=adapter.worker_id,
        contract_id=contract.id,
        claim_runtime_records=claim.runtime_records,
    )
    assert claimed is not None

    proposal = adapter.result_candidate(
        claimed.package,
        _action(),
        usage_metadata={"adapter": "openalex", "network": False},
    )
    assert submit_worker_proposal(proposal, store)["status"] == "accepted"
    store.close()

    reopened = SQLiteStore(db_path)
    restored = reopened.get_contract(contract.id)
    assert restored is not None
    view = RuntimeProjection(reopened, reopened).contract_view(restored)
    output = reopened.get_assets_by_name("retrieval_manifest")[-1]

    assert view.terminal == "complete"
    assert view.outputs_satisfied is True
    assert json.loads(output.content)["returned_count"] == 2
