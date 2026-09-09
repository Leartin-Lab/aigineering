"""Run the reusable-method governance flow with an explicit fixture Worker."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

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
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.protocol.types import Candidate
from aigineering.runtime import claim_next_package, execute_claimed_package


class _FixtureWorker:
    def __init__(self, worker_id: str, *, generated: dict | None = None) -> None:
        self.worker_id = worker_id
        self.generated = generated

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(self.worker_id)

    def invoke(self, contract, disclosed_assets):
        if self.generated is not None:
            content = json.dumps(self.generated, separators=(",", ":"))
        else:
            binding = json.loads(contract.description.splitlines()[-1])
            source_id = binding["inputs"]["source"]
            source = next(asset for asset in disclosed_assets if asset.id == source_id)
            values = json.loads(source.content)
            matched = values["source"] == values["reported"]
            content = json.dumps(
                {
                    "matched": matched,
                    "reason": "source and reported values match"
                    if matched
                    else "source and reported values differ",
                },
                separators=(",", ":"),
            )
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps({"outputs": {contract.outputs[0]: content}}),
        )


def _publish_asset(store, asset):
    decision = require_accepted(
        commit_local_effects(
            store,
            (asset_proposal_effect(asset),),
            idempotency_key=f"example:asset:{asset.id}",
        )
    )
    return decision.assets[0]


def _execute(store, task, worker) -> None:
    host = ensure_local_worker_host(store, worker)
    claimed = claim_next_package(store, worker_id=host.worker_id, contract_id=task.id)
    if claimed is None:
        raise RuntimeError(f"Contract {task.id} was not claimable")
    execute_claimed_package(claimed, host, store)


def run(directory: Path) -> dict[str, object]:
    directory.mkdir(parents=True)
    os.chdir(directory)
    db_path = directory / ".aig" / "store.db"
    store = SQLiteStore(str(db_path))
    try:
        ensure_local_domain(store)
        service = MethodService(
            store,
            lambda effects, **kwargs: commit_local_effects(store, effects, **kwargs),
        )
        package_text = (Path(__file__).with_name("method.json")).read_text(
            encoding="utf-8"
        )
        cases_text = (Path(__file__).with_name("cases.json")).read_text(
            encoding="utf-8"
        )

        request = _publish_asset(
            store,
            build_control_plane_asset(
                name="request",
                content="Create a method comparing source and reported numbers.",
            ),
        )
        generation = service.create(
            request.id, output="generated_method", name="author-method", budget=2
        )
        _execute(
            store,
            generation,
            _FixtureWorker("worker:method-author", generated=json.loads(package_text)),
        )
        method = next(
            asset
            for asset in store.get_assets_by_name("generated_method")
            if asset.created_by == generation.id
        )
        suite = service.import_cases(cases_text, "heldout-counts")
        run_asset, root = service.prepare_tests(
            method.id, suite.id, name="method-tests", budget=1
        )
        for case in json.loads(run_asset.content)["cases"]:
            _execute(
                store,
                store.get_contract(case["contract_id"]),
                _FixtureWorker("worker:method-fixture"),
            )
        evaluation, assessment = service.assess(run_asset.id)
        publisher = ensure_local_plugin_publisher(
            store, "method.review.v1", ("asset.attest", "method.verify")
        )
        require_accepted(
            publisher.publish(
                (service.attestation_effect(evaluation.id),),
                idempotency_key=f"example:attest:{evaluation.id}",
            )
        )
        source = _publish_asset(
            store,
            build_control_plane_asset(
                name="live-source", content='{"source": 3, "reported": 4}'
            ),
        )
        reuse = service.instantiate(
            method.id,
            inputs={"source": source.id},
            outputs={"assessment": "live-assessment"},
            name="reuse-method",
            budget=1,
            evaluation_id=evaluation.id,
        )
        _execute(
            store,
            reuse,
            _FixtureWorker("worker:method-fixture"),
        )
        reuse_outputs = [
            asset
            for asset in store.get_assets_by_name("live-assessment")
            if asset.created_by == reuse.id
        ]
        if len(reuse_outputs) != 1 or json.loads(reuse_outputs[0].content) != {
            "matched": False,
            "reason": "source and reported values differ",
        }:
            raise RuntimeError("fixture reuse output did not match the expected result")
        before = store.runtime_materialization_digest()
        store.close()
        store = SQLiteStore(str(db_path))
        rebuilt = store.rebuild_runtime_materializations()
        if rebuilt != before:
            raise RuntimeError("runtime materialization digest changed after rebuild")
        return {
            "status": "complete",
            "request_id": request.id,
            "method_id": method.id,
            "test_run_id": run_asset.id,
            "test_contract_id": root.id,
            "evaluation_id": evaluation.id,
            "assessment_contract_id": assessment.id,
            "reuse_contract_id": reuse.id,
            "rebuild_match": True,
        }
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("fixture",), default=None)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if args.worker != "fixture":
        parser.error("--worker fixture is required; no Worker runs by default")
    directory = args.directory.expanduser().resolve()
    if directory.exists():
        parser.error(f"refusing to overwrite existing directory: {directory}")
    print(json.dumps(run(directory), sort_keys=True))


if __name__ == "__main__":
    main()
