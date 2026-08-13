"""Mechanically verify and independently attest one AI4S literature report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from aigineering.core.ids import acceptance_policy_id
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_plugin_publisher
from aigineering.protocol.effect_builders import asset_attestation_effect


def verify_report(
    report_content: str, manifests: list[dict[str, Any]]
) -> dict[str, Any]:
    """Bind every claimed citation to a stable ID returned by the tool."""
    report = json.loads(report_content)
    if not isinstance(report, dict):
        raise ValueError("report must be a JSON object")
    answer = report.get("answer")
    citations = report.get("citations")
    limitations = report.get("limitations")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("report.answer must be a non-empty string")
    if not isinstance(citations, list) or not citations:
        raise ValueError("report.citations must be a non-empty list")
    if not all(isinstance(item, str) and item for item in citations):
        raise ValueError("report.citations must contain stable string IDs")
    if len(citations) != len(set(citations)):
        raise ValueError("report.citations must not contain duplicate IDs")
    if not isinstance(limitations, list) or not all(
        isinstance(item, str) and item for item in limitations
    ):
        raise ValueError("report.limitations must be a list of non-empty strings")
    retrieved_ids = {
        str(record["id"])
        for manifest in manifests
        for record in manifest.get("records", [])
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    unknown = sorted(set(citations) - retrieved_ids)
    if unknown:
        raise ValueError(f"report cites IDs absent from retrieved evidence: {unknown}")
    return {
        "citation_count": len(citations),
        "retrieved_id_count": len(retrieved_ids),
        "verified_citation_ids": sorted(citations),
    }


def attest_report(db_path: str, contract_id: str) -> dict[str, Any]:
    """Verify descendant evidence and publish a distinct signed attestation."""
    store = SQLiteStore(db_path)
    try:
        contract = store.get_contract(contract_id)
        if contract is None:
            raise ValueError(f"unknown task {contract_id!r}")
        policy = contract.acceptance_policy
        if policy is None or policy.get("mode") != "independent":
            raise ValueError("AI4S report task requires independent acceptance")
        reports = [
            asset
            for name in contract.outputs
            for asset in store.get_assets_by_name(name)
            if _is_descendant(asset.created_by, contract.id, store)
        ]
        if len(reports) != 1:
            raise ValueError(
                f"expected exactly one descendant report, found {len(reports)}"
            )
        manifests, evidence_asset_ids = _retrieval_manifests(contract.id, store)
        if not manifests:
            raise ValueError("no successful OpenAlex observation was committed")
        evidence = verify_report(reports[0].content, manifests)
        publisher = ensure_local_plugin_publisher(
            store,
            "ai4s.literature.verify.v1",
            ("asset.attest", "verify.literature"),
        )
        decision = publisher.publish(
            (
                asset_attestation_effect(
                    contract.id,
                    reports[0].name,
                    reports[0].id,
                    policy_id=acceptance_policy_id(policy),
                    policy_version=str(policy["policy_version"]),
                    rubric_asset_ids=tuple(policy.get("rubric_asset_ids", ())),
                    evidence_asset_ids=tuple(policy.get("evidence_asset_ids", ())),
                ),
            ),
            idempotency_key=f"ai4s-attest:{contract.id}:{reports[0].id}",
            causal_parents=(reports[0].id, *evidence_asset_ids),
        )
        if not decision.accepted:
            raise ValueError("independent AI4S attestation Candidate was rejected")
        return {
            "accepted": True,
            "contract_id": contract.id,
            "report_asset_id": reports[0].id,
            "observation_asset_ids": evidence_asset_ids,
            **evidence,
        }
    finally:
        store.close()


def _retrieval_manifests(contract_id: str, store) -> tuple[list[dict], tuple[str, ...]]:
    manifests: list[dict] = []
    asset_ids: list[str] = []
    for asset in store.get_all_assets():
        if not _is_descendant(asset.created_by, contract_id, store):
            continue
        try:
            observation = json.loads(asset.content)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(observation, dict)
            or observation.get("ok") is not True
            or observation.get("tool") != "openalex_search"
        ):
            continue
        result = json.loads(str(observation.get("result", "")))
        if isinstance(result, dict) and result.get("source") == "openalex":
            manifests.append(result)
            asset_ids.append(asset.id)
    return manifests, tuple(asset_ids)


def _is_descendant(producer_id: str, contract_id: str, store) -> bool:
    current_id = producer_id
    visited: set[str] = set()
    while current_id and current_id not in visited:
        if current_id == contract_id:
            return True
        visited.add(current_id)
        current = store.get_contract(current_id)
        current_id = str(current.parent_id or "") if current is not None else ""
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=".aig/store.db")
    parser.add_argument("--task", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = attest_report(args.db, args.task)
    document = json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(document + "\n", encoding="utf-8")
    print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
