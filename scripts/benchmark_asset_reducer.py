#!/usr/bin/env python3
"""Measure signed Asset publication costs against closed signed history."""

from __future__ import annotations

import argparse
import cProfile
import dataclasses
import hashlib
import json
import platform
import pstats
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parents[1]


def _nearest_rank_p95(values: list[float]) -> float:
    if not values:
        raise ValueError("p95 requires at least one observation")
    ordered = sorted(values)
    return ordered[(95 * len(ordered) + 99) // 100 - 1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package_imports(package_root: Path) -> dict[str, Any]:
    package_root = package_root.resolve()
    package_dir = package_root / "aigineering"
    if not (package_dir / "__init__.py").is_file():
        raise RuntimeError("package root must contain aigineering/__init__.py")
    for name, module in tuple(sys.modules.items()):
        if name == "aigineering" or name.startswith("aigineering."):
            origin = getattr(module, "__file__", None)
            if origin is None or not Path(origin).resolve().is_relative_to(package_dir):
                raise RuntimeError("aigineering was already imported from another root")
    sys.path[:] = [
        item for item in sys.path if Path(item or ".").resolve() != package_root
    ]
    sys.path.insert(0, str(package_root))
    from aigineering.core.candidate_publisher import publish_effects
    from aigineering.core.control_plane import (
        build_control_plane_asset,
        build_control_plane_contract,
    )
    from aigineering.core.domain import initialize_genesis
    from aigineering.core.signing import Ed25519Signer
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.agent.mock import MockWorker
    from aigineering.agent.worker import WorkerHost
    from aigineering.core.worker_routing import WorkerRegistration
    from aigineering.protocol.candidate import (
        ActorKey,
        create_candidate_proposal,
        create_genesis_manifest,
    )
    from aigineering.protocol.effect_builders import (
        asset_attestation_effect,
        asset_proposal_effect,
        contract_cancellation_effect,
        contract_declaration_effect,
        worker_registration_effect,
    )
    from aigineering.runtime import claim_next_package, execute_claimed_package
    from aigineering.core.acceptance import acceptance_policy_id
    from aigineering.protocol.identity import contract_from_fields

    import aigineering.core.commitment as commitment
    import aigineering.core.fact_materialization as materialization
    import aigineering.core.projection_context as projection_context

    if (
        not Path(__import__("aigineering").__file__)
        .resolve()
        .is_relative_to(package_dir)
    ):
        raise RuntimeError("selected package root did not win import resolution")
    for name, module in tuple(sys.modules.items()):
        if name == "aigineering" or name.startswith("aigineering."):
            origin = getattr(module, "__file__", None)
            if origin is None or not Path(origin).resolve().is_relative_to(package_dir):
                raise RuntimeError(
                    f"loaded module {name!r} escaped the selected package root"
                )
    return {
        "publish_effects": publish_effects,
        "build_control_plane_asset": build_control_plane_asset,
        "build_control_plane_contract": build_control_plane_contract,
        "initialize_genesis": initialize_genesis,
        "Ed25519Signer": Ed25519Signer,
        "SQLiteStore": SQLiteStore,
        "MockWorker": MockWorker,
        "WorkerHost": WorkerHost,
        "WorkerRegistration": WorkerRegistration,
        "ActorKey": ActorKey,
        "create_candidate_proposal": create_candidate_proposal,
        "create_genesis_manifest": create_genesis_manifest,
        "asset_proposal_effect": asset_proposal_effect,
        "asset_attestation_effect": asset_attestation_effect,
        "contract_cancellation_effect": contract_cancellation_effect,
        "contract_declaration_effect": contract_declaration_effect,
        "worker_registration_effect": worker_registration_effect,
        "claim_next_package": claim_next_package,
        "execute_claimed_package": execute_claimed_package,
        "acceptance_policy_id": acceptance_policy_id,
        "contract_from_fields": contract_from_fields,
        "commitment": commitment,
        "materialization": materialization,
        "projection_context": projection_context,
    }


def _decision_signature(decision):
    return (
        bool(decision.accepted),
        tuple(item.id for item in decision.assets),
        tuple(item.id for item in decision.contracts),
        tuple(item.id for item in decision.runtime_records),
        tuple(item.id for item in decision.trace_entries),
    )


def _publish_twinned(
    store,
    signer,
    key,
    genesis,
    effects,
    name: str,
    imports,
    instrumented_path: Path,
) -> tuple[Any, dict[str, Any], Any]:
    candidate = imports["create_candidate_proposal"](
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=tuple(effects),
        signer=signer,
        idempotency_key=name,
    )
    timing_path = Path(store._db_path)
    instrumented = _snapshot(timing_path, instrumented_path, imports)
    from aigineering.core.commitment import CandidateCommitter

    started = time.perf_counter()
    decision = CandidateCommitter(store, store).commit(candidate, genesis)
    timing_ms = (time.perf_counter() - started) * 1000
    setup = _instrument(instrumented, imports)
    started = time.perf_counter()
    twin_decision = CandidateCommitter(instrumented, instrumented).commit(
        candidate, genesis
    )
    counts = _restore(instrumented, setup)
    instrumented_ms = (time.perf_counter() - started) * 1000
    signatures_equal = _decision_signature(decision) == _decision_signature(
        twin_decision
    )
    timing_digest = _semantic_digest(store)
    twin_digest = _semantic_digest(instrumented)
    rebuild_equal = _rebuild_digest_matches(
        store, timing_path, imports
    ) and _rebuild_digest_matches(instrumented, instrumented_path, imports)
    if (
        not decision.accepted
        or not twin_decision.accepted
        or not signatures_equal
        or timing_digest != twin_digest
        or not rebuild_equal
    ):
        raise RuntimeError(
            "signed publication timing and instrumentation twins diverged"
        )
    instrumented.close()
    return (
        decision,
        {
            "same_signed_candidate_replayed": True,
            "accepted": bool(decision.accepted),
            "decision_signatures_equal": signatures_equal,
            "semantic_digest_equal": timing_digest == twin_digest,
            "read_only_rebuild_digest_equal": rebuild_equal,
            "timing_ms": timing_ms,
            "instrumented_ms": instrumented_ms,
            "instrumentation": counts,
        },
        candidate,
    )


def _manifest(package_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(package_dir).as_posix(): _sha(path)
        for path in sorted(package_dir.rglob("*.py"))
        if path.is_file()
    }


def _new_store(path: Path, imports: dict[str, Any]):
    store = imports["SQLiteStore"](str(path))
    signer = imports["Ed25519Signer"]()
    worker_signer = imports["Ed25519Signer"]()
    verifier_signer = imports["Ed25519Signer"]()
    key = imports["ActorKey"](
        "benchmark:authority",
        "authority-key",
        signer.kind,
        signer.signer_id,
        ("asset.publish", "contract.publish", "contract.cancel", "worker.register"),
    )
    worker_key = imports["ActorKey"](
        "benchmark:worker",
        "worker-key",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    verifier_key = imports["ActorKey"](
        "benchmark:verifier",
        "verifier-key",
        verifier_signer.kind,
        verifier_signer.signer_id,
        ("asset.attest", "asset.publish", "policy.review", "worker.submit"),
    )
    genesis = imports["create_genesis_manifest"](
        "asset-reducer-benchmark",
        (key, worker_key, verifier_key),
        "policy:asset-reducer-v1",
    )
    imports["initialize_genesis"](store, genesis)
    registration = imports["WorkerRegistration"](
        worker_id=worker_key.actor_id,
        actor_id=worker_key.actor_id,
        key_id=worker_key.key_id,
        version="1",
    )
    registered = _publish(
        store,
        signer,
        key,
        genesis,
        (imports["worker_registration_effect"](registration),),
        "worker-registration",
        imports,
    )
    if not registered.accepted:
        raise RuntimeError("signed Worker registration was rejected")
    verifier_registration = imports["WorkerRegistration"](
        worker_id=verifier_key.actor_id,
        capabilities=("policy.review",),
        actor_id=verifier_key.actor_id,
        key_id=verifier_key.key_id,
        version="1",
    )
    verifier_registered = _publish(
        store,
        signer,
        key,
        genesis,
        (imports["worker_registration_effect"](verifier_registration),),
        "verifier-registration",
        imports,
    )
    if not verifier_registered.accepted:
        raise RuntimeError("signed verifier registration was rejected")
    host = imports["WorkerHost"](
        imports["MockWorker"](worker_id=worker_key.actor_id),
        genesis,
        worker_key,
        worker_signer,
        registration_version="1",
    )
    return store, signer, key, genesis, host, verifier_signer, verifier_key


def _publish(store, signer, key, genesis, effects, name: str, imports):
    return imports["publish_effects"](
        store,
        store,
        genesis,
        key,
        signer,
        tuple(effects),
        idempotency_key=name,
    )


def _build_prefix(store, signer, key, genesis, history: int, imports, host):
    baseline_asset_ids = {asset.id for asset in store.get_all_assets()}
    baseline_runtime_ids = {
        record.id for _revision, record in store.scan_runtime_records()
    }
    baseline_trace_ids = {entry.id for entry in store.get_trace_events()}
    baseline_claim_ids = {
        str(row[0]) for row in store._conn.execute("SELECT claim_id FROM worker_claims")
    }
    assets = []
    effects = []
    contracts = []
    for index in range(history):
        source = imports["build_control_plane_asset"](
            name=f"closed_source_{index}",
            content=f"source {index}",
            origin="human",
            trust_tier="human",
        )
        contract = imports["build_control_plane_contract"](
            name=f"closed_contract_{index}",
            inputs=(source.name,),
            outputs=(f"closed_output_{index}",),
            activation=source.name,
            budget=1,
        )
        assets.append(source)
        contracts.append(contract)
        effects.extend(
            (
                imports["asset_proposal_effect"](source),
                imports["contract_declaration_effect"](contract),
            )
        )
    if effects:
        result = _publish(
            store, signer, key, genesis, effects, f"prefix:{history}", imports
        )
        if not result.accepted:
            raise RuntimeError("signed history declaration rejected")
        worker = host.worker
        for index, contract in enumerate(contracts):
            worker.set_output(
                contract.name, f'/exec {{"outputs":{{"closed_output_{index}":"ok"}}}}'
            )
            claim_candidate = host.claim_candidate(
                request_id=f"history-claim:{index}", contract_id=contract.id
            )
            claimed = imports["claim_next_package"](
                store,
                worker_id=host.worker_id,
                contract_id=contract.id,
                expected_registration_version=host.registration_version,
                claim_candidate=claim_candidate,
            )
            if claimed is None:
                raise RuntimeError("signed history task could not be claimed")
            imports["execute_claimed_package"](claimed, host, store)
    history_runtime_ids = {
        record.id for _revision, record in store.scan_runtime_records()
    } - baseline_runtime_ids
    history_trace_ids = {
        entry.id for entry in store.get_trace_events()
    } - baseline_trace_ids
    history_claim_ids = {
        str(row[0]) for row in store._conn.execute("SELECT claim_id FROM worker_claims")
    } - baseline_claim_ids
    active_contracts = [
        imports["build_control_plane_contract"](
            name=f"fixed_active_{index}",
            inputs=(),
            outputs=(f"never_produced_{index}",),
            activation="changed_signal",
            budget=1,
        )
        for index in range(2)
    ]
    decision = _publish(
        store,
        signer,
        key,
        genesis,
        tuple(
            imports["contract_declaration_effect"](item) for item in active_contracts
        ),
        f"active-fixed:{history}",
        imports,
    )
    if not decision.accepted:
        raise RuntimeError("fixed-A signed declarations were rejected")
    records = store.scan_runtime_records(record_type="lifecycle.terminal")
    terminal = {
        str(record.payload.get("contract_id"))
        for _revision, record in records
        if record.payload.get("terminal") == "complete"
    }
    if not {contract.id for contract in contracts}.issubset(terminal):
        raise RuntimeError(
            "signed prefix history did not complete through the normal lifecycle"
        )
    persisted_assets = store.get_all_assets()
    history_asset_ids = {asset.id for asset in persisted_assets} - baseline_asset_ids
    history_source_asset_ids = {
        asset.id for source in assets for asset in store.get_assets_by_name(source.name)
    }
    history_output_asset_ids = {
        asset.id
        for index in range(history)
        for asset in store.get_assets_by_name(f"closed_output_{index}")
    }
    if (
        len(history_source_asset_ids) != history
        or len(history_output_asset_ids) != history
        or history_asset_ids != history_source_asset_ids | history_output_asset_ids
    ):
        raise RuntimeError("closed history Asset identity classification is incomplete")
    claim_rows = {
        str(row[1]): str(row[0])
        for row in store._conn.execute(
            "SELECT claim_id, contract_id FROM worker_claims"
        )
        if str(row[0]) in history_claim_ids
    }
    registrations = store.get_worker_registrations()
    return {
        "contracts": len(contracts),
        "assets": len(history_asset_ids),
        "source_assets": len(history_source_asset_ids),
        "output_assets": len(history_output_asset_ids),
        "terminal": len(terminal),
        "active_contracts": len(active_contracts),
        "_classification": {
            # Include both the source Assets and normal-lifecycle output
            # Assets from the signed closed-H prefix.
            "history_asset_ids": sorted(history_asset_ids),
            "history_source_asset_ids": sorted(history_source_asset_ids),
            "history_output_asset_ids": sorted(history_output_asset_ids),
            "history_contract_ids": [contract.id for contract in contracts],
            "history_runtime_record_ids": sorted(history_runtime_ids),
            "history_trace_ids": sorted(history_trace_ids),
            "history_claim_ids": sorted(history_claim_ids),
            "history_claim_contract_ids": sorted(claim_rows),
            "active_contract_ids": [contract.id for contract in active_contracts],
            "registration_worker_ids": [
                registration.worker_id for registration in registrations
            ],
        },
    }


def _snapshot(source: Path, target: Path, imports):
    base = imports["SQLiteStore"](str(source))
    twin = imports["SQLiteStore"](str(target))
    try:
        base._conn.backup(twin._conn)
    finally:
        base.close()
    return twin


def _rebuild_digest_matches(store, path: Path, imports) -> bool:
    copy = _snapshot(path, path.with_name(path.stem + "-rebuild.sqlite"), imports)
    try:
        before = copy.runtime_materialization_digest()
        rebuilt = copy.rebuild_runtime_materializations(strict=True)
        after = copy.runtime_materialization_digest()
        return before == rebuilt == after
    finally:
        copy.close()


def _candidate_effects(size: int, imports, *, mixed: bool = False):
    assets = [
        imports["build_control_plane_asset"](
            name="changed_signal" if index == 0 else f"nontrigger_{index}",
            content=f"measured content {index}",
            origin="human",
            trust_tier="human",
        )
        for index in range(size)
    ]
    effects = [imports["asset_proposal_effect"](asset) for asset in assets]
    contracts = []
    if mixed:
        contract = imports["build_control_plane_contract"](
            name="same_batch_contract",
            inputs=("changed_signal",),
            outputs=("unproduced_output",),
            activation="changed_signal",
            budget=1,
        )
        contracts.append(contract)
        effects.insert(0, imports["contract_declaration_effect"](contract))
    return tuple(effects), assets, contracts


def _microcases(root: Path, imports) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=False)
    observed: dict[str, Any] = {}
    for case in (
        "asset_before_contract",
        "contract_before_asset",
        "activation_conjunction_missing_one",
        "same_batch_asset_before_declaration",
        "same_batch_declaration_before_satisfying_asset",
        "duplicate_same_name_same_content",
        "duplicate_same_name_sequential_publications",
        "signed_intermediate_cancellation_after_descendant_declaration",
    ):
        case_dir = root / case
        case_dir.mkdir(parents=True, exist_ok=False)
        store, signer, key, genesis, _host, _verifier_signer, _verifier_key = (
            _new_store(case_dir / "facts.sqlite", imports)
        )
        if case == "asset_before_contract":
            asset = imports["build_control_plane_asset"](
                name="early_signal", content="early", origin="human", trust_tier="human"
            )
            asset_result = _publish(
                store,
                signer,
                key,
                genesis,
                (imports["asset_proposal_effect"](asset),),
                "early-asset",
                imports,
            )
            contract = imports["build_control_plane_contract"](
                name="early_contract",
                outputs=("never_early",),
                activation="early_signal",
                budget=1,
            )
            contract_result, twin, _candidate = _publish_twinned(
                store,
                signer,
                key,
                genesis,
                (imports["contract_declaration_effect"](contract),),
                "late-declare",
                imports,
                case_dir / "instrumented.sqlite",
            )
            observed[case] = {
                "accepted": bool(asset_result.accepted and contract_result.accepted),
                "assets_before_contract": len(store.get_assets_by_name("early_signal"))
                == 1,
                "declared_contracts": len(store.get_all_contracts()),
                "twins": twin,
            }
        elif case == "contract_before_asset":
            contract = imports["build_control_plane_contract"](
                name="late_contract",
                outputs=("never_late",),
                activation="late_signal",
                budget=1,
            )
            declared = _publish(
                store,
                signer,
                key,
                genesis,
                (imports["contract_declaration_effect"](contract),),
                "early-declare",
                imports,
            )
            asset = imports["build_control_plane_asset"](
                name="late_signal", content="late", origin="human", trust_tier="human"
            )
            added, twin, _candidate = _publish_twinned(
                store,
                signer,
                key,
                genesis,
                (imports["asset_proposal_effect"](asset),),
                "late-asset",
                imports,
                case_dir / "instrumented.sqlite",
            )
            observed[case] = {
                "accepted": bool(declared.accepted and added.accepted),
                "activation_trace": any(
                    entry.event_type == "activation" for entry in added.trace_entries
                ),
                "twins": twin,
            }
        elif case == "activation_conjunction_missing_one":
            contract = imports["build_control_plane_contract"](
                name="conjunction",
                outputs=("never_conjunction",),
                activation="left_signal AND right_signal",
                budget=1,
            )
            declared = _publish(
                store,
                signer,
                key,
                genesis,
                (imports["contract_declaration_effect"](contract),),
                "conjunction-declare",
                imports,
            )
            left = imports["build_control_plane_asset"](
                name="left_signal", content="left", origin="human", trust_tier="human"
            )
            left_result = _publish(
                store,
                signer,
                key,
                genesis,
                (imports["asset_proposal_effect"](left),),
                "left-only",
                imports,
            )
            right = imports["build_control_plane_asset"](
                name="right_signal", content="right", origin="human", trust_tier="human"
            )
            right_result, twin, _candidate = _publish_twinned(
                store,
                signer,
                key,
                genesis,
                (imports["asset_proposal_effect"](right),),
                "right-added",
                imports,
                case_dir / "instrumented.sqlite",
            )
            observed[case] = {
                "accepted": bool(
                    declared.accepted and left_result.accepted and right_result.accepted
                ),
                "left_did_not_activate": not any(
                    entry.event_type == "activation"
                    for entry in left_result.trace_entries
                ),
                "both_names_activated": any(
                    entry.event_type == "activation"
                    for entry in right_result.trace_entries
                ),
                "twins": twin,
            }
        elif case == "same_batch_asset_before_declaration":
            trigger = imports["build_control_plane_asset"](
                name="overlay_signal",
                content="overlay",
                origin="human",
                trust_tier="human",
            )
            output_asset = imports["build_control_plane_asset"](
                name="overlay_output",
                content="business assertion from human origin",
                origin="human",
                trust_tier="human",
            )
            contract = imports["build_control_plane_contract"](
                name="overlay_contract",
                inputs=("overlay_signal",),
                outputs=("overlay_output",),
                activation="overlay_signal",
                budget=1,
            )
            result, twin, _candidate = _publish_twinned(
                store,
                signer,
                key,
                genesis,
                (
                    imports["asset_proposal_effect"](trigger),
                    imports["asset_proposal_effect"](output_asset),
                    imports["contract_declaration_effect"](contract),
                ),
                "asset-before-declare-overlay",
                imports,
                case_dir / "instrumented.sqlite",
            )
            terminal = {
                str(record.payload.get("contract_id")): str(
                    record.payload.get("terminal")
                )
                for _revision, record in store.scan_runtime_records(
                    record_type="lifecycle.terminal"
                )
            }
            observed[case] = {
                "accepted": bool(result.accepted),
                "accepted_asset_count": len(result.assets),
                "accepted_contract_count": len(result.contracts),
                "activation_trace": any(
                    entry.event_type == "activation" for entry in result.trace_entries
                ),
                "output_satisfied_trace": any(
                    entry.event_type == "output_satisfied"
                    for entry in result.trace_entries
                ),
                "contract_terminal": terminal.get(contract.id),
                "assertion_scope": "default-policy human-origin output assertion, not independent truth",
                "twins": twin,
            }
        elif case == "same_batch_declaration_before_satisfying_asset":
            trigger = imports["build_control_plane_asset"](
                name="overlay_signal_reverse",
                content="reverse trigger",
                origin="human",
                trust_tier="human",
            )
            output_asset = imports["build_control_plane_asset"](
                name="overlay_output_reverse",
                content="business assertion from human origin",
                origin="human",
                trust_tier="human",
            )
            contract = imports["build_control_plane_contract"](
                name="overlay_contract_reverse",
                inputs=("overlay_signal_reverse",),
                outputs=("overlay_output_reverse",),
                activation="overlay_signal_reverse",
                budget=1,
            )
            result, twin, _candidate = _publish_twinned(
                store,
                signer,
                key,
                genesis,
                (
                    imports["contract_declaration_effect"](contract),
                    imports["asset_proposal_effect"](trigger),
                    imports["asset_proposal_effect"](output_asset),
                ),
                "declare-before-assets-overlay",
                imports,
                case_dir / "instrumented.sqlite",
            )
            terminal = {
                str(record.payload.get("contract_id")): str(
                    record.payload.get("terminal")
                )
                for _revision, record in store.scan_runtime_records(
                    record_type="lifecycle.terminal"
                )
            }
            observed[case] = {
                "accepted": bool(result.accepted),
                "accepted_asset_count": len(result.assets),
                "accepted_contract_count": len(result.contracts),
                "activation_trace": any(
                    entry.event_type == "activation" for entry in result.trace_entries
                ),
                "output_satisfied_trace": any(
                    entry.event_type == "output_satisfied"
                    for entry in result.trace_entries
                ),
                "contract_terminal": terminal.get(contract.id),
                "assertion_scope": "default-policy human-origin output assertion, not independent truth",
                "twins": twin,
            }
        elif case == "duplicate_same_name_same_content":
            first = imports["build_control_plane_asset"](
                name="duplicate_signal",
                content="same",
                origin="human",
                trust_tier="human",
            )
            second = imports["build_control_plane_asset"](
                name="duplicate_signal",
                content="same",
                origin="human",
                trust_tier="human",
            )
            result, twin, _candidate = _publish_twinned(
                store,
                signer,
                key,
                genesis,
                (
                    imports["asset_proposal_effect"](first),
                    imports["asset_proposal_effect"](second),
                ),
                "duplicate-effects",
                imports,
                case_dir / "instrumented.sqlite",
            )
            accepted_assets = tuple(result.assets)
            observed[case] = {
                "accepted": bool(result.accepted),
                "effect_count": 2,
                "decision_asset_count": len(accepted_assets),
                "unique_asset_ids": len({asset.id for asset in accepted_assets}),
                "twins": twin,
            }
        elif case == "duplicate_same_name_sequential_publications":
            first = imports["build_control_plane_asset"](
                name="sequential_duplicate_signal",
                content="same sequential content",
                origin="human",
                trust_tier="human",
            )
            second = imports["build_control_plane_asset"](
                name="sequential_duplicate_signal",
                content="same sequential content",
                origin="human",
                trust_tier="human",
            )
            first_candidate = imports["create_candidate_proposal"](
                domain_id=genesis.id,
                actor_id=key.actor_id,
                key_id=key.key_id,
                effects=(imports["asset_proposal_effect"](first),),
                signer=signer,
                idempotency_key="sequential-duplicate-first",
            )
            from aigineering.core.commitment import CandidateCommitter

            first_result = CandidateCommitter(store, store).commit(
                first_candidate, genesis
            )
            second_result, twin, second_candidate = _publish_twinned(
                store,
                signer,
                key,
                genesis,
                (imports["asset_proposal_effect"](second),),
                "sequential-duplicate-second",
                imports,
                case_dir / "instrumented.sqlite",
            )
            unique_ids = {
                asset.id
                for item in (first_result, second_result)
                for asset in item.assets
            }
            observed[case] = {
                "accepted": bool(first_result.accepted and second_result.accepted),
                "publication_count": 2,
                "candidate_ids_distinct": first_candidate.id != second_candidate.id,
                "effect_count": 2,
                "decision_asset_count": sum(
                    len(item.assets) for item in (first_result, second_result)
                ),
                "unique_asset_ids": len(unique_ids),
                "twins": twin,
            }
        else:
            parent = imports["build_control_plane_contract"](
                name="cancel_parent", outputs=("parent_output",), budget=3
            )
            child = imports["contract_from_fields"](
                name="cancel_intermediate",
                description="A declared intermediate to cancel.",
                parent_id=parent.id,
                inputs=(),
                outputs=("intermediate_output",),
                activation="",
                budget=2,
                tool_scope=(),
                labels=(),
                origin="human",
            )
            grandchild = imports["contract_from_fields"](
                name="cancel_grandchild",
                description="A declared descendant under the intermediate.",
                parent_id=child.id,
                inputs=(),
                outputs=("grandchild_output",),
                activation="",
                budget=1,
                tool_scope=(),
                labels=(),
                origin="human",
            )
            declarations = []
            for contract in (parent, child, grandchild):
                declarations.append(
                    _publish(
                        store,
                        signer,
                        key,
                        genesis,
                        (imports["contract_declaration_effect"](contract),),
                        f"cancel-case-declare:{contract.name}",
                        imports,
                    )
                )
            cancelled = _publish(
                store,
                signer,
                key,
                genesis,
                (
                    imports["contract_cancellation_effect"](
                        child.id, "signed benchmark cancellation"
                    ),
                ),
                "signed-intermediate-cancel",
                imports,
            )
            terminals = {
                str(record.payload.get("contract_id")): str(
                    record.payload.get("terminal")
                )
                for _revision, record in store.scan_runtime_records(
                    record_type="lifecycle.terminal"
                )
            }
            observed[case] = {
                "accepted": all(item.accepted for item in declarations)
                and bool(cancelled.accepted),
                "intermediate_signed_cancelled": terminals.get(child.id) == "cancelled",
                "descendant_terminal_observed": grandchild.id in terminals,
            }
            parent_output = imports["build_control_plane_asset"](
                name="parent_output",
                content="parent completion assertion from human origin",
                origin="human",
                trust_tier="human",
            )
            parent_completion, twin, _candidate = _publish_twinned(
                store,
                signer,
                key,
                genesis,
                (imports["asset_proposal_effect"](parent_output),),
                "cancelled-intermediate-parent-output",
                imports,
                case_dir / "instrumented.sqlite",
            )
            final_terminals = {
                str(record.payload.get("contract_id")): str(
                    record.payload.get("terminal")
                )
                for _revision, record in store.scan_runtime_records(
                    record_type="lifecycle.terminal"
                )
            }
            observed[case].update(
                {
                    "accepted": observed[case]["accepted"]
                    and bool(parent_completion.accepted),
                    "parent_completed_after_output": final_terminals.get(parent.id)
                    == "complete",
                    "cancelled_intermediate_preserved": final_terminals.get(child.id)
                    == "cancelled",
                    "unfinished_grandchild_terminal": final_terminals.get(
                        grandchild.id
                    ),
                    "twins": twin,
                }
            )
        store.close()
    if not all(item.get("accepted") for item in observed.values()):
        raise RuntimeError(f"a signed correctness microcase was rejected: {observed}")
    if not observed["contract_before_asset"]["activation_trace"]:
        raise RuntimeError("late Asset did not trigger Contract activation")
    if not observed["activation_conjunction_missing_one"]["left_did_not_activate"]:
        raise RuntimeError("incomplete multi-name activation was reported active")
    if not observed["activation_conjunction_missing_one"]["both_names_activated"]:
        raise RuntimeError("complete multi-name activation was not reported")
    if not observed["same_batch_asset_before_declaration"]["activation_trace"]:
        raise RuntimeError(
            "Asset-first same-batch declaration overlay did not activate"
        )
    if not observed["signed_intermediate_cancellation_after_descendant_declaration"][
        "intermediate_signed_cancelled"
    ]:
        raise RuntimeError("signed cancellation did not terminalize the intermediate")
    for case in (
        "same_batch_asset_before_declaration",
        "same_batch_declaration_before_satisfying_asset",
    ):
        if (
            observed[case]["contract_terminal"] != "complete"
            or not observed[case]["output_satisfied_trace"]
        ):
            raise RuntimeError(f"same-batch human output assertion failed: {case}")
    if not observed["duplicate_same_name_sequential_publications"][
        "candidate_ids_distinct"
    ]:
        raise RuntimeError(
            "sequential duplicate microcase did not use distinct Candidates"
        )
    observed["descendant_and_independent_acceptance"] = _cascade_acceptance_microcase(
        root / "descendant-and-independent", imports
    )
    return observed


def _cascade_acceptance_microcase(root: Path, imports) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=False)
    store, signer, operator, genesis, producer, verifier_signer, verifier = _new_store(
        root / "facts.sqlite", imports
    )
    policy = {
        "mode": "independent",
        "policy_version": "benchmark-verifier-v1",
        "required_attestations": 1,
        "verifier_capabilities": ["policy.review"],
    }
    parent = imports["build_control_plane_contract"](
        name="independent_root",
        outputs=("root_final",),
        budget=5,
        acceptance_policy=policy,
    )
    child = imports["contract_from_fields"](
        name="completed_intermediate",
        description="Complete one intermediate output.",
        parent_id=parent.id,
        inputs=(),
        outputs=("child_done",),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        origin="human",
    )
    review = imports["contract_from_fields"](
        name="independent_verifier",
        description="Review the root output.",
        parent_id=child.id,
        inputs=("root_final",),
        outputs=("review_receipt",),
        activation="root_final",
        budget=1,
        tool_scope=(),
        labels=(),
        worker_capabilities=("policy.review",),
        origin="human",
    )
    sibling = imports["contract_from_fields"](
        name="unfinished_sibling",
        description="Remain unfinished until root acceptance.",
        parent_id=parent.id,
        inputs=(),
        outputs=("sibling_done",),
        activation="",
        budget=1,
        tool_scope=(),
        labels=(),
        origin="human",
    )
    for contract in (parent, child, review, sibling):
        declarations = _publish(
            store,
            signer,
            operator,
            genesis,
            (imports["contract_declaration_effect"](contract),),
            f"cascade-declare:{contract.name}",
            imports,
        )
        if not declarations.accepted:
            raise RuntimeError("signed cascade declaration rejected")

    def execute(host, contract, output, request):
        host.worker.set_output(
            contract.name, f'/exec {{"outputs":{{"{output}":"ok"}}}}'
        )
        claim_candidate = host.claim_candidate(
            request_id=request, contract_id=contract.id
        )
        claimed = imports["claim_next_package"](
            store,
            worker_id=host.worker_id,
            contract_id=contract.id,
            expected_registration_version=host.registration_version,
            claim_candidate=claim_candidate,
        )
        if claimed is None:
            raise RuntimeError("signed cascade claim was rejected")
        imports["execute_claimed_package"](claimed, host, store)

    execute(producer, child, "child_done", "cascade-child-claim")
    child_terminal = [
        record
        for _revision, record in store.scan_runtime_records(
            record_type="lifecycle.terminal"
        )
        if record.payload.get("contract_id") == child.id
    ]
    if (
        len(child_terminal) != 1
        or child_terminal[0].payload.get("terminal") != "complete"
    ):
        raise RuntimeError("intermediate did not reach its signed complete terminal")
    if store.get_claim(review.id) is not None:
        raise RuntimeError("verifier branch unexpectedly claimed before root output")
    execute(producer, parent, "root_final", "cascade-root-claim")
    if any(
        record.payload.get("contract_id") == parent.id
        for _revision, record in store.scan_runtime_records(
            record_type="lifecycle.terminal"
        )
    ):
        raise RuntimeError("independent root completed before verifier attestation")
    verifier_host = imports["WorkerHost"](
        imports["MockWorker"](worker_id=verifier.actor_id),
        genesis,
        verifier,
        verifier_signer,
        registration_version="1",
    )
    verifier_claim = verifier_host.claim_candidate(
        request_id="cascade-verifier-claim", contract_id=review.id
    )
    claimed = imports["claim_next_package"](
        store,
        worker_id=verifier_host.worker_id,
        contract_id=review.id,
        expected_registration_version=verifier_host.registration_version,
        claim_candidate=verifier_claim,
    )
    if claimed is None:
        raise RuntimeError(
            "live verifier branch could not claim after root output: "
            f"claim={store.get_claim(review.id)!r}; "
            f"terminals={[record.payload for _, record in store.scan_runtime_records(record_type='lifecycle.terminal') if record.payload.get('contract_id') == review.id]!r}"
        )
    output_asset = store.get_assets_by_name("root_final")[0]
    attestation = imports["asset_attestation_effect"](
        parent.id,
        "root_final",
        output_asset.id,
        policy_id=imports["acceptance_policy_id"](policy),
        policy_version="benchmark-verifier-v1",
        verdict="accepted",
    )
    qualified, twin, _candidate = _publish_twinned(
        store,
        verifier_signer,
        verifier,
        genesis,
        (attestation,),
        "verify-root-output",
        imports,
        root / "instrumented-attestation.sqlite",
    )
    if not qualified.accepted:
        raise RuntimeError("signed independent attestation was rejected")
    terminals = {
        str(record.payload.get("contract_id")): str(record.payload.get("terminal"))
        for _revision, record in store.scan_runtime_records(
            record_type="lifecycle.terminal"
        )
    }
    observed = {
        "root_output_existed_before_attestation": output_asset is not None,
        "root_completed_after_attestation": terminals.get(parent.id) == "complete",
        "completed_intermediate_preserved": terminals.get(child.id) == "complete",
        "claimed_verifier_cancelled_after_acceptance": terminals.get(review.id)
        == "cancelled"
        and store.get_claim(review.id).get("status") == "released",
        "unfinished_sibling_cancelled": terminals.get(sibling.id) == "cancelled",
        "attestation_twin": twin,
        "prefix_setup_scope": "signed child completion, root output, and verifier claim precede backup; only final attestation is timed",
    }
    store.close()
    if not all(observed.values()):
        raise RuntimeError(f"signed cascade/acceptance invariant failed: {observed}")
    return observed


def _semantic_digest(store) -> str:
    def plain(value):
        if dataclasses.is_dataclass(value):
            return {
                field.name: plain(getattr(value, field.name))
                for field in dataclasses.fields(value)
            }
        if isinstance(value, dict) or hasattr(value, "items"):
            return {str(key): plain(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [plain(item) for item in value]
        return value

    contracts = [plain(item) for item in store.get_all_contracts()]
    assets = [plain(item) for item in store.get_all_assets()]
    records = []
    for _revision, record in store.scan_runtime_records():
        item = plain(record)
        item.pop("recorded_at", None)
        records.append(item)
    trace = []
    for entry in store.get_trace_events():
        item = plain(entry)
        item.pop("timestamp", None)
        trace.append(item)
    body = json.dumps(
        {"contracts": contracts, "assets": assets, "records": records, "trace": trace},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(body).hexdigest()


def _classify_entity(classification, entity: str, identifier: object) -> str:
    if identifier is None:
        return "unknown_or_scalar"
    identifier = str(identifier)
    if identifier in classification.get(f"history_{entity}_ids", set()):
        return "known_history_H"
    if identifier in classification.get(f"related_{entity}_ids", set()):
        return "current_related"
    if entity == "registration_worker" and identifier in classification.get(
        "registration_worker_ids", set()
    ):
        return "control_registration_W"
    if entity == "claim_contract" and identifier in classification.get(
        "history_claim_contract_ids", set()
    ):
        return "known_history_H"
    if entity == "claim_contract" and identifier in classification.get(
        "related_claim_contract_ids", set()
    ):
        return "current_related"
    return "unknown_or_scalar"


def _classify_sqlite_row(row, classification) -> str:
    columns = set(row.keys())
    if {"record_id", "record_type", "payload_json"}.issubset(columns):
        category = _classify_entity(classification, "runtime_record", row["record_id"])
        if category != "unknown_or_scalar":
            return category
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            return "unknown_or_scalar"
        candidate_id = (
            payload.get("candidate_id") if isinstance(payload, dict) else None
        )
        if candidate_id is not None and str(candidate_id) in classification.get(
            "related_candidate_ids", set()
        ):
            return "current_related"
        return "unknown_or_scalar"
    if {"id", "parent_id", "inputs", "outputs"}.issubset(columns):
        return _classify_entity(classification, "contract", row["id"])
    if {"id", "content_hash", "trust_tier"}.issubset(columns):
        return _classify_entity(classification, "asset", row["id"])
    if {"id", "event_type", "timestamp", "contract_id"}.issubset(columns):
        return _classify_entity(classification, "trace", row["id"])
    if {"claim_id", "contract_id", "status", "worker_id"}.issubset(columns):
        return _classify_entity(classification, "claim", row["claim_id"])
    if {"worker_id", "actor_id", "key_id", "capabilities"}.issubset(columns):
        return _classify_entity(classification, "registration_worker", row["worker_id"])
    return "unknown_or_scalar"


def _store_result_classification(method: str, result, classification) -> dict[str, int]:
    if method == "get_all_contracts":
        items = (("contract", item.id) for item in result)
    elif method == "get_all_assets":
        items = (("asset", item.id) for item in result)
    elif method == "scan_runtime_records":
        items = (("runtime_record", record.id) for _revision, record in result)
    elif method == "get_trace_events":
        items = (("trace", item.id) for item in result)
    elif method == "get_worker_registrations":
        items = (("registration_worker", item.worker_id) for item in result)
    elif method == "get_claim_history_contract_ids":
        items = (("claim_contract", item) for item in result)
    else:
        items = ()
    counts: dict[str, int] = {}
    for entity, identifier in items:
        category = _classify_entity(classification, entity, identifier)
        counts[category] = counts.get(category, 0) + 1
    return counts


def _context_loader_capabilities(imports):
    """Return the actual entry API and its selected module path."""
    commitment = imports["commitment"]
    projection_context = imports["projection_context"]
    capabilities = {
        "projection_context.load_candidate_projection_context": callable(
            getattr(projection_context, "load_candidate_projection_context", None)
        ),
        "projection_context.load_asset_proposal_projection_context": callable(
            getattr(projection_context, "load_asset_proposal_projection_context", None)
        ),
        "projection_context.load_contract_declaration_projection_context": callable(
            getattr(
                projection_context,
                "load_contract_declaration_projection_context",
                None,
            )
        ),
        "commitment.load_contract_declaration_projection_context": callable(
            getattr(commitment, "load_contract_declaration_projection_context", None)
        ),
    }
    if capabilities["projection_context.load_candidate_projection_context"]:
        if getattr(commitment, "context_inputs", None) is not projection_context:
            raise RuntimeError(
                "CandidateCommitter context_inputs is not the selected projection_context module"
            )
        active_entry_path = "candidate_facade"
    elif capabilities["commitment.load_contract_declaration_projection_context"]:
        active_entry_path = "contract_declaration"
    else:
        raise RuntimeError("required Candidate context entry loader is absent")
    return capabilities, active_entry_path


def _instrument(store, imports, classification=None):
    classification = classification or {}
    counts: dict[str, Any] = {
        "store_calls": {},
        "store_returned_rows": {},
        "stage_store_calls": {},
        "stage_returned_rows": {},
        "sqlite_rows_fetched": 0,
        "stage_sql_rows_fetched": {},
        "sqlite_row_classes": {},
        "stage_sql_row_classes": {},
        "stage_store_row_classes": {},
        "decoded_rows": {
            "contracts": 0,
            "assets": 0,
            "runtime_records": 0,
            "registrations": 0,
        },
        "stage_decoded_rows": {},
        "context_loader_calls": 0,
        "full_projection_context_calls": 0,
        "full_projection_context_rows": {
            "contracts": 0,
            "assets": 0,
            "runtime_records": 0,
            "registrations": 0,
            "claim_history_contract_ids": 0,
        },
        "context_rows": {
            "contracts": 0,
            "assets": 0,
            "runtime_records": 0,
            "registrations": 0,
            "claim_history_contract_ids": 0,
        },
        "context_loader_api_capabilities": {},
        "context_loader_entry_path": None,
        "context_loader_path_calls": {
            "candidate_facade": 0,
            "contract_declaration": 0,
        },
        "context_loader_result_summaries": [],
        "pre_commit_reducer_calls": 0,
        "writer_reducer_calls": 0,
        "sql_selects": 0,
        "sqlite_vm_progress_callbacks": 0,
        "sqlite_vm_interval": 1000,
        "profile_total_calls": 0,
        "stage_duration_ms": {},
        "writer_ingress_consequences_calls": 0,
    }
    active_stage = "other_commit_work"
    restores = []
    original_row_factory = store._conn.row_factory

    def counting_row_factory(cursor, row):
        counts["sqlite_rows_fetched"] += 1
        stages = counts["stage_sql_rows_fetched"]
        stages[active_stage] = stages.get(active_stage, 0) + 1
        if original_row_factory is None:
            result = row
        else:
            result = original_row_factory(cursor, row)
        category = _classify_sqlite_row(result, classification)
        counts["sqlite_row_classes"][category] = (
            counts["sqlite_row_classes"].get(category, 0) + 1
        )
        stage_classes = counts["stage_sql_row_classes"].setdefault(active_stage, {})
        stage_classes[category] = stage_classes.get(category, 0) + 1
        return result

    store._conn.row_factory = counting_row_factory
    restores.append((store._conn, "row_factory", original_row_factory))
    for name in (
        "get_all_contracts",
        "get_all_assets",
        "scan_runtime_records",
        "get_worker_registrations",
        "get_claim_history_contract_ids",
        "get_trace_events",
    ):
        original = getattr(store, name)

        def wrapped(*args, __original=original, __name=name, **kwargs):
            result = __original(*args, **kwargs)
            counts["store_calls"][__name] = counts["store_calls"].get(__name, 0) + 1
            counts["store_returned_rows"][__name] = counts["store_returned_rows"].get(
                __name, 0
            ) + len(result)
            stage = counts["stage_store_calls"].setdefault(active_stage, {})
            stage[__name] = stage.get(__name, 0) + 1
            rows = counts["stage_returned_rows"].setdefault(active_stage, {})
            rows[__name] = rows.get(__name, 0) + len(result)
            classified = _store_result_classification(__name, result, classification)
            method_classes = (
                counts["stage_store_row_classes"]
                .setdefault(active_stage, {})
                .setdefault(__name, {})
            )
            for category, amount in classified.items():
                method_classes[category] = method_classes.get(category, 0) + amount
            return result

        setattr(store, name, wrapped)
        restores.append((store, name, original))
    commitment = imports["commitment"]
    projection_context = imports["projection_context"]
    capabilities, active_entry_path = _context_loader_capabilities(imports)
    counts["context_loader_api_capabilities"] = capabilities
    counts["context_loader_entry_path"] = active_entry_path
    modules = []
    if active_entry_path == "candidate_facade":
        modules.append(
            (
                projection_context,
                "load_candidate_projection_context",
                "context",
                "candidate_facade",
            )
        )
    else:
        modules.append(
            (
                commitment,
                "load_contract_declaration_projection_context",
                "context",
                "contract_declaration",
            )
        )
    modules.extend(
        (
            (
                projection_context,
                "load_effect_projection_context",
                "full",
                None,
            ),
            (imports["commitment"], "reduce_asset_facts", "pre", None),
            (imports["materialization"], "reduce_asset_facts", "writer", None),
        )
    )
    wrapped_functions = set()
    for module, name, stage, path_name in modules:
        function_key = (id(module), name)
        if function_key in wrapped_functions:
            raise RuntimeError(f"benchmark instrumentation would double-wrap {name}")
        wrapped_functions.add(function_key)
        original = getattr(module, name)

        def wrapped(
            *args,
            __original=original,
            __stage=stage,
            __path_name=path_name,
            **kwargs,
        ):
            nonlocal active_stage
            previous_stage = active_stage
            start = time.perf_counter()
            active_stage = {
                "context": "projection_context_total",
                "full": "full_projection_context",
                "pre": "pre_commit_reduce_asset_facts",
                "writer": "writer_reduce_asset_facts",
            }[__stage]
            if __stage == "full":
                counts["full_projection_context_calls"] += 1
                try:
                    result = __original(*args, **kwargs)
                    for field in counts["full_projection_context_rows"]:
                        counts["full_projection_context_rows"][field] += len(
                            getattr(result, field)
                        )
                    return result
                finally:
                    active_stage = previous_stage
                    elapsed = (time.perf_counter() - start) * 1000
                    counts["stage_duration_ms"]["full_projection_context"] = (
                        counts["stage_duration_ms"].get("full_projection_context", 0.0)
                        + elapsed
                    )
            if __stage == "context":
                counts["context_loader_calls"] += 1
                counts["context_loader_path_calls"][__path_name] += 1
                try:
                    result = __original(*args, **kwargs)
                    for field in counts["context_rows"]:
                        counts["context_rows"][field] += len(getattr(result, field))
                    runtime_records = tuple(getattr(result, "runtime_records"))
                    related_ids = classification.get(
                        "related_runtime_record_ids", set()
                    )
                    related_records = tuple(
                        record for record in runtime_records if record.id in related_ids
                    )
                    counts["context_loader_result_summaries"].append(
                        {
                            "path": __path_name,
                            "runtime_record_rows": len(runtime_records),
                            "related_runtime_record_rows": len(related_records),
                            "related_runtime_record_types": dict(
                                sorted(
                                    Counter(
                                        record.record_type for record in related_records
                                    ).items()
                                )
                            ),
                            "related_runtime_record_ids_sha256": hashlib.sha256(
                                "\n".join(
                                    sorted(record.id for record in related_records)
                                ).encode()
                            ).hexdigest(),
                        }
                    )
                    return result
                finally:
                    active_stage = previous_stage
                    elapsed = (time.perf_counter() - start) * 1000
                    counts["stage_duration_ms"]["projection_context_total"] = (
                        counts["stage_duration_ms"].get("projection_context_total", 0.0)
                        + elapsed
                    )
            elif __stage == "pre":
                counts["pre_commit_reducer_calls"] += 1
            else:
                counts["writer_reducer_calls"] += 1
            try:
                return __original(*args, **kwargs)
            finally:
                active_stage = previous_stage
                stage_name = (
                    "pre_commit_reduce_asset_facts"
                    if __stage == "pre"
                    else "writer_reduce_asset_facts"
                )
                counts["stage_duration_ms"][stage_name] = (
                    counts["stage_duration_ms"].get(stage_name, 0.0)
                    + (time.perf_counter() - start) * 1000
                )

        setattr(module, name, wrapped)
        restores.append((module, name, original))
    for method, counter in (
        ("_row_to_contract", "contracts"),
        ("_row_to_asset", "assets"),
        ("_row_to_runtime_record", "runtime_records"),
        ("_row_to_worker_registration", "registrations"),
    ):
        original = getattr(store, method)

        def decoded(row, __original=original, __counter=counter):
            counts["decoded_rows"][__counter] += 1
            stages = counts["stage_decoded_rows"].setdefault(active_stage, {})
            stages[__counter] = stages.get(__counter, 0) + 1
            return __original(row)

        setattr(store, method, decoded)
        restores.append((store, method, original))
    original_ingress = store._commit_time_ingress_consequences

    def ingress(*args, **kwargs):
        nonlocal active_stage
        previous_stage = active_stage
        active_stage = "writer_ingress_consequences_total"
        counts["writer_ingress_consequences_calls"] += 1
        start = time.perf_counter()
        try:
            return original_ingress(*args, **kwargs)
        finally:
            active_stage = previous_stage
            counts["stage_duration_ms"]["writer_ingress_consequences_total"] = (
                counts["stage_duration_ms"].get(
                    "writer_ingress_consequences_total", 0.0
                )
                + (time.perf_counter() - start) * 1000
            )

    store._commit_time_ingress_consequences = ingress
    restores.append((store, "_commit_time_ingress_consequences", original_ingress))

    def trace(sql: str):
        if sql.lstrip().upper().startswith(("SELECT", "WITH")):
            counts["sql_selects"] += 1

    store._conn.set_trace_callback(trace)

    def progress():
        counts["sqlite_vm_progress_callbacks"] += 1
        return 0

    store._conn.set_progress_handler(progress, counts["sqlite_vm_interval"])
    profiler = cProfile.Profile()
    profiler.enable()
    return counts, restores, profiler


def _restore(store, setup):
    counts, restores, profiler = setup
    profiler.disable()
    counts["profile_total_calls"] = pstats.Stats(profiler).total_calls
    store._conn.set_trace_callback(None)
    store._conn.set_progress_handler(None, 0)
    for owner, name, original in reversed(restores):
        setattr(owner, name, original)
    return counts


def _explain_plans(store) -> dict[str, list[str]]:
    queries = {
        "all_contracts": ("SELECT * FROM contracts ORDER BY id", ()),
        "asset_name": (
            "SELECT * FROM assets WHERE name = ? ORDER BY id",
            ("changed_signal",),
        ),
        "terminal_records": (
            "SELECT * FROM runtime_records WHERE record_type = ? ORDER BY revision",
            ("lifecycle.terminal",),
        ),
    }
    return {
        name: [
            str(row[3])[:200]
            for row in store._conn.execute("EXPLAIN QUERY PLAN " + sql, params)
        ]
        for name, (sql, params) in queries.items()
    }


def _run_case(
    size: int, history: int, mode: str, output_dir: Path, imports, *, mixed=False
):
    output_dir.mkdir(parents=True, exist_ok=False)
    prefix_path = output_dir / "prefix.sqlite"
    domain_started = time.perf_counter()
    setup, signer, key, genesis, host, _verifier_signer, _verifier_key = _new_store(
        prefix_path, imports
    )
    domain_setup_ms = (time.perf_counter() - domain_started) * 1000
    history_started = time.perf_counter()
    history_info = _build_prefix(setup, signer, key, genesis, history, imports, host)
    history_build_ms = (time.perf_counter() - history_started) * 1000
    effects, assets, _contracts = _candidate_effects(size, imports, mixed=mixed)
    candidates = []
    from aigineering.protocol.candidate import create_candidate_proposal

    if mode == "asset_batch" or mixed:
        candidates.append(
            create_candidate_proposal(
                domain_id=genesis.id,
                actor_id=key.actor_id,
                key_id=key.key_id,
                effects=effects,
                signer=signer,
                idempotency_key=f"measured:{mode}:{history}:{size}",
            )
        )
    else:
        for index, effect in enumerate(effects):
            candidates.append(
                create_candidate_proposal(
                    domain_id=genesis.id,
                    actor_id=key.actor_id,
                    key_id=key.key_id,
                    effects=(effect,),
                    signer=signer,
                    idempotency_key=f"measured:{mode}:{history}:{size}:{index}",
                )
            )
    setup.close()
    timing_path = output_dir / "timing.sqlite"
    twin_path = output_dir / "instrumented.sqlite"
    timing = _snapshot(prefix_path, timing_path, imports)
    instrumented = _snapshot(prefix_path, twin_path, imports)
    from aigineering.core.commitment import CandidateCommitter

    start = time.perf_counter()
    decisions = [
        CandidateCommitter(timing, timing).commit(c, genesis) for c in candidates
    ]
    elapsed_ms = (time.perf_counter() - start) * 1000
    history_classification = history_info.pop("_classification")
    expected_candidate_count = 1 if mode == "asset_batch" or mixed else size
    accepted_asset_effect_count = sum(len(decision.assets) for decision in decisions)
    accepted_asset_ids = {
        asset.id for decision in decisions for asset in decision.assets
    }
    if (
        len(candidates) != expected_candidate_count
        or len(decisions) != expected_candidate_count
        or not all(decision.accepted for decision in decisions)
        or accepted_asset_effect_count != len(assets)
        or len(accepted_asset_ids) != len(assets)
    ):
        raise RuntimeError(
            "measured publication count or accepted Asset identities differ from the fixture"
        )
    measurement_contracts = tuple(_contracts)
    classification = {
        "history_asset_ids": set(history_classification["history_asset_ids"]),
        "history_contract_ids": set(history_classification["history_contract_ids"]),
        "history_runtime_record_ids": set(
            history_classification["history_runtime_record_ids"]
        ),
        "history_trace_ids": set(history_classification["history_trace_ids"]),
        "history_claim_ids": set(history_classification["history_claim_ids"]),
        "history_claim_contract_ids": set(
            history_classification["history_claim_contract_ids"]
        ),
        # Candidate builders also carry proposal identifiers. The decision
        # contains the actual canonical Asset facts written by publication.
        "related_asset_ids": {
            asset.id for decision in decisions for asset in decision.assets
        },
        "related_contract_ids": set(history_classification["active_contract_ids"])
        | {contract.id for contract in measurement_contracts},
        "related_runtime_record_ids": {
            record.id for decision in decisions for record in decision.runtime_records
        },
        "related_trace_ids": {
            entry.id for decision in decisions for entry in decision.trace_entries
        },
        "related_candidate_ids": {candidate.id for candidate in candidates},
        "registration_worker_ids": set(
            history_classification["registration_worker_ids"]
        ),
        "related_claim_contract_ids": set(),
    }
    setup_counts = _instrument(instrumented, imports, classification)
    instrumented_started = time.perf_counter()
    twin_decisions = [
        CandidateCommitter(instrumented, instrumented).commit(c, genesis)
        for c in candidates
    ]
    counts = _restore(instrumented, setup_counts)
    instrumented_elapsed_ms = (time.perf_counter() - instrumented_started) * 1000
    counts["sqlite_vm_work_proxy"] = (
        counts["sqlite_vm_progress_callbacks"] * counts["sqlite_vm_interval"]
    )
    accepted = all(item.accepted for item in decisions)
    twin_accepted = all(item.accepted for item in twin_decisions)
    digests_equal = _semantic_digest(timing) == _semantic_digest(instrumented)

    decisions_equal = [_decision_signature(item) for item in decisions] == [
        _decision_signature(item) for item in twin_decisions
    ]
    rebuild_matches = _rebuild_digest_matches(
        timing, timing_path, imports
    ) and _rebuild_digest_matches(instrumented, twin_path, imports)
    if (
        not accepted
        or not twin_accepted
        or not digests_equal
        or not decisions_equal
        or not rebuild_matches
    ):
        raise RuntimeError("timing and instrumentation twins diverged or rejected")
    all_contracts = timing.get_all_contracts()
    terminal_ids = {
        str(record.payload.get("contract_id"))
        for _revision, record in timing.scan_runtime_records(
            record_type="lifecycle.terminal"
        )
    }
    active_count = sum(contract.id not in terminal_ids for contract in all_contracts)
    fixed_active_ids = set(history_classification["active_contract_ids"])
    if (
        len(fixed_active_ids) != history_info["active_contracts"]
        or not fixed_active_ids.issubset({contract.id for contract in all_contracts})
        or fixed_active_ids & terminal_ids
    ):
        raise RuntimeError("fixed-A sparse obligations unexpectedly completed")
    activation_fanout = sum(
        "changed_signal" in contract.activation.split("+") for contract in all_contracts
    )
    contract_by_id = {contract.id: contract for contract in all_contracts}
    relevant_names = set()
    contract_reference_counts = []
    for contract in all_contracts:
        relevant_names.update(contract.inputs)
        activation = contract.activation.replace(" AND ", "+")
        activation_refs = [
            part.strip() for part in activation.split("+") if part.strip()
        ]
        relevant_names.update(activation_refs)
        contract_reference_counts.append(
            {
                "contract_name": contract.name,
                "activation_refs": len(activation_refs),
                "input_refs": len(contract.inputs),
                "output_refs": len(contract.outputs),
                "parented": contract.parent_id is not None,
            }
        )
    assets_by_name: dict[str, int] = {}
    for asset in timing.get_all_assets():
        assets_by_name[asset.name] = assets_by_name.get(asset.name, 0) + 1
    same_name_fanout = max(
        (assets_by_name.get(name, 0) for name in relevant_names), default=0
    )
    descendants = [
        contract for contract in all_contracts if contract.parent_id is not None
    ]

    def contract_depth(contract_id: str) -> int:
        depth = 0
        seen: set[str] = set()
        current = contract_by_id.get(contract_id)
        while current is not None and current.parent_id is not None:
            if current.id in seen:
                raise RuntimeError("measured Contract ancestry contains a cycle")
            seen.add(current.id)
            depth += 1
            current = contract_by_id.get(current.parent_id)
        return depth

    descendant_depth = max(
        (contract_depth(contract.id) for contract in descendants), default=0
    )
    publication_attempts = [
        {"accepted": bool(decision.accepted), "effect_count": len(candidate.effects)}
        for decision, candidate in zip(decisions, candidates, strict=True)
    ]
    query_plans = _explain_plans(instrumented)
    inclusive_stage_ms = sum(
        counts["stage_duration_ms"].get(name, 0.0)
        for name in (
            "projection_context_total",
            "pre_commit_reduce_asset_facts",
            "writer_ingress_consequences_total",
        )
    )
    counts["instrumented_publication_total_ms"] = instrumented_elapsed_ms
    counts["other_commit_work_ms_estimate"] = max(
        0.0, instrumented_elapsed_ms - inclusive_stage_ms
    )
    timing.close()
    instrumented.close()
    return {
        "mode": mode,
        "P": size,
        "H": history,
        "A": active_count,
        "history": history_info,
        "history_identity_counts": {
            "assets_H": len(history_classification["history_asset_ids"]),
            "contracts_H": len(history_classification["history_contract_ids"]),
            "runtime_records_H": len(
                history_classification["history_runtime_record_ids"]
            ),
            "trace_events_H": len(history_classification["history_trace_ids"]),
            "worker_claim_rows_H": len(history_classification["history_claim_ids"]),
            "registration_workers_W": len(
                history_classification["registration_worker_ids"]
            ),
        },
        "domain_setup_ms": domain_setup_ms,
        "history_build_ms": history_build_ms,
        "actual_unclosed_contracts": active_count,
        "fixed_sparse_contract_count": len(fixed_active_ids),
        "fixed_sparse_contracts_unterminal": not bool(fixed_active_ids & terminal_ids),
        "activation_fanout": activation_fanout,
        "same_name_fanout": same_name_fanout,
        "descendant_count": len(descendants),
        "descendant_depth": descendant_depth,
        "contract_reference_counts": contract_reference_counts,
        "unrelated_history_rows_by_stage": {
            "store_returned_objects": {
                stage: {
                    method: classes.get("known_history_H", 0)
                    for method, classes in methods.items()
                }
                for stage, methods in counts["stage_store_row_classes"].items()
            },
            "sqlite_materialized_rows": {
                stage: classes.get("known_history_H", 0)
                for stage, classes in counts["stage_sql_row_classes"].items()
            },
        },
        "row_classification_by_stage": {
            "store_returned_objects": counts["stage_store_row_classes"],
            "sqlite_materialized_rows": counts["stage_sql_row_classes"],
        },
        "accepted_publications": len(decisions),
        "attempted_publications": len(publication_attempts),
        "accepted_effects": sum(len(decision.assets) for decision in decisions),
        "accepted_contracts": sum(len(decision.contracts) for decision in decisions),
        "activation_events": sum(
            entry.event_type == "activation"
            for decision in decisions
            for entry in decision.trace_entries
        ),
        "unique_asset_ids": len(
            {asset.id for decision in decisions for asset in decision.assets}
        ),
        "decision_asset_count": sum(len(decision.assets) for decision in decisions),
        "publication_attempts": publication_attempts,
        "explain_query_plan": query_plans,
        "timing_ms": elapsed_ms,
        "instrumentation": counts,
        "twin_semantics_equal": digests_equal,
        "twin_decisions_equal": decisions_equal,
        "read_only_rebuild_digest_equal": rebuild_matches,
    }


def _run_exact_replay_probe(output_dir: Path, imports) -> dict[str, Any]:
    """Measure one exact P=1 replay bundle after a normally signed H=16 prefix."""
    output_dir.mkdir(parents=True, exist_ok=False)
    prefix_path = output_dir / "prefix.sqlite"
    setup, signer, key, genesis, host, _verifier_signer, _verifier_key = _new_store(
        prefix_path, imports
    )
    history = _build_prefix(setup, signer, key, genesis, 16, imports, host)
    effects, _assets, _contracts = _candidate_effects(1, imports)
    candidate = imports["create_candidate_proposal"](
        domain_id=genesis.id,
        actor_id=key.actor_id,
        key_id=key.key_id,
        effects=effects,
        signer=signer,
        idempotency_key="d0c:exact-replay-probe",
    )
    from aigineering.core.commitment import CandidateCommitter

    initial = CandidateCommitter(setup, setup).commit(candidate, genesis)
    all_records = tuple(record for _revision, record in setup.scan_runtime_records())
    receipts = tuple(
        record
        for record in all_records
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate.id
    )
    heads = tuple(
        record
        for record in all_records
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == candidate.id
    )
    if not initial.accepted or len(receipts) != 1 or len(heads) != 1:
        setup.close()
        raise RuntimeError("H=16 replay fixture lacks its unique receipt or head")
    member_ids = heads[0].payload.get("committed_record_ids")
    if type(member_ids) not in (list, tuple) or len(member_ids) != 2:
        setup.close()
        raise RuntimeError("H=16 replay fixture head does not name two members")
    member_records = tuple(
        record for record in all_records if record.id in set(member_ids)
    )
    if len(member_records) != 2 or len(set(member_ids)) != 2:
        setup.close()
        raise RuntimeError("H=16 replay fixture members are not unique and present")
    proof_records = (*receipts, *heads, *member_records)
    if len(proof_records) != 4:
        setup.close()
        raise RuntimeError("H=16 replay fixture did not select four proof records")
    proof_ids = {record.id for record in proof_records}
    proof_digest = hashlib.sha256("\n".join(sorted(proof_ids)).encode()).hexdigest()
    expected_types = dict(
        sorted(Counter(record.record_type for record in proof_records).items())
    )
    proof_roles = {
        "receipt_rows": len(receipts),
        "head_rows": len(heads),
        "member_rows": len(member_records),
        "member_ids_sha256": hashlib.sha256(
            "\n".join(sorted(member_ids)).encode()
        ).hexdigest(),
    }
    before_digest = _semantic_digest(setup)
    setup.close()

    replay_path = output_dir / "replay.sqlite"
    replay_store = _snapshot(prefix_path, replay_path, imports)
    history_classification = history.pop("_classification")
    classification = {
        "history_asset_ids": set(history_classification["history_asset_ids"]),
        "history_contract_ids": set(history_classification["history_contract_ids"]),
        "history_runtime_record_ids": set(
            history_classification["history_runtime_record_ids"]
        ),
        "history_trace_ids": set(history_classification["history_trace_ids"]),
        "history_claim_ids": set(history_classification["history_claim_ids"]),
        "history_claim_contract_ids": set(
            history_classification["history_claim_contract_ids"]
        ),
        "related_runtime_record_ids": proof_ids,
        "related_candidate_ids": {candidate.id},
        "registration_worker_ids": set(
            history_classification["registration_worker_ids"]
        ),
    }
    setup_counts = _instrument(replay_store, imports, classification)
    replay = CandidateCommitter(replay_store, replay_store).commit(candidate, genesis)
    counts = _restore(replay_store, setup_counts)
    after_digest = _semantic_digest(replay_store)
    replay_store.close()

    summaries = counts["context_loader_result_summaries"]
    expected_path = counts["context_loader_entry_path"]
    if (
        not replay.accepted
        or before_digest != after_digest
        or len(summaries) != 1
        or summaries[0]["path"] != expected_path
        or summaries[0]["related_runtime_record_rows"] != 4
        or summaries[0]["related_runtime_record_ids_sha256"] != proof_digest
        or summaries[0]["related_runtime_record_types"] != expected_types
    ):
        raise RuntimeError("H=16 exact replay did not return the original proof bundle")
    if expected_path == "candidate_facade":
        if (
            summaries[0]["runtime_record_rows"] != 4
            or counts["full_projection_context_calls"] != 0
        ):
            raise RuntimeError("narrow replay context included non-proof history")
    elif (
        counts["full_projection_context_calls"] != 1
        or counts["context_rows"]["runtime_records"]
        != counts["full_projection_context_rows"]["runtime_records"]
    ):
        raise RuntimeError(
            "legacy replay context did not retain its full-history route"
        )

    counts["sqlite_vm_work_proxy"] = (
        counts["sqlite_vm_progress_callbacks"] * counts["sqlite_vm_interval"]
    )
    return {
        "H": 16,
        "P": 1,
        "observation_kind": "separate_exact_replay_not_a_context_pair",
        "history_assets": history["assets"],
        "initial_publication_accepted": bool(initial.accepted),
        "replay_accepted": bool(replay.accepted),
        "store_unchanged_by_replay": before_digest == after_digest,
        "expected_proof_runtime_record_rows": len(proof_records),
        "expected_proof_runtime_record_types": expected_types,
        "expected_proof_roles": proof_roles,
        "expected_proof_record_ids_sha256": proof_digest,
        "reader_path": expected_path,
        "context_loader_result_summaries": summaries,
        "instrumentation": counts,
    }


def _args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--package-root", type=Path, default=SCRIPT_ROOT / "src")
    parser.add_argument(
        "--source-revision",
        help="caller-supplied label; it does not verify imported source identity",
    )
    parser.add_argument(
        "--expected-package-manifest-sha256",
        help="optional exact check for the complete loaded package Python-file map",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--smoke", action="store_true")
    group.add_argument("--matrix", action="store_true")
    group.add_argument(
        "--context-compat",
        action="store_true",
        help="run six focused legacy/new projection-context compatibility pairs",
    )
    return parser


def _mode_parameters(args) -> dict[str, Any]:
    if args.smoke:
        return {
            "mode": "smoke",
            "P": [2],
            "H": [0, 1],
            "modes": ["asset_batch", "asset_sequential", "mixed_asset_declare"],
            "timing_samples_per_P_H_mode": 1,
            "context_pairs": 0,
        }
    if args.context_compat:
        return {
            "mode": "context_compat",
            "P": [1, 64],
            "H": [0, 16, 64],
            "modes": ["asset_only", "mixed_asset_declare"],
            "timing_samples_per_P_H_mode": 1,
            "context_pairs": 6,
            "separate_replay_probe": {"P": 1, "H": 16, "timing_pair": False},
        }
    return {
        "mode": "matrix",
        "P": [8, 16, 32, 64],
        "H": [0, 16, 64],
        "modes": ["asset_batch", "asset_sequential"],
        "timing_samples_per_P_H_mode": 3,
        "context_pairs": 6,
    }


def main() -> int:
    parser = _args()
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite existing report")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    work = None
    keep_work = False
    results = []
    phase = "package_import"
    failed_cell = None
    manifest = {}
    package_manifest_sha = None
    context_loader_api_capabilities = {}
    context_loader_entry_path = None
    replay_probe = None
    try:
        imports = _package_imports(args.package_root)
        package_dir = Path(__import__("aigineering").__path__[0]).resolve()
        phase = "package_manifest"
        manifest = _manifest(package_dir)
        if len(manifest) < 200:
            raise RuntimeError("loaded package manifest is incomplete")
        package_manifest_sha = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()
        if (
            args.expected_package_manifest_sha256 is not None
            and package_manifest_sha != args.expected_package_manifest_sha256
        ):
            raise RuntimeError(
                "loaded package does not match the expected package manifest"
            )
        (
            context_loader_api_capabilities,
            context_loader_entry_path,
        ) = _context_loader_capabilities(imports)
        phase = "workspace_setup"
        work = Path(tempfile.mkdtemp(prefix="asset-reducer-"))
        phase = "correctness_microcases" if args.smoke else "measurement_cells"
        microcases = _microcases(work / "microcases", imports) if args.smoke else {}
        if args.smoke:
            cases = [
                (2, 1, "asset_batch", False, 0),
                (2, 1, "asset_sequential", False, 0),
                (2, 0, "asset_batch", True, 0),
            ]
        elif args.context_compat:
            cases = [(64, h, "asset_only", False, 0) for h in (0, 16, 64)] + [
                (1, h, "mixed_asset_declare", True, 0) for h in (0, 16, 64)
            ]
        else:
            cases = [
                (p, h, mode, False, sample)
                for mode in ("asset_batch", "asset_sequential")
                for p in (8, 16, 32, 64)
                for h in (0, 16, 64)
                for sample in range(3)
            ]
            cases.extend((1, h, "asset_only", False, 0) for h in (0, 16, 64))
            cases.extend((1, h, "mixed_asset_declare", True, 0) for h in (0, 16, 64))
        results = []
        timing_ms = []
        for index, (p, h, mode, mixed, sample) in enumerate(cases):
            failed_cell = {
                "index": index,
                "P": p,
                "H": h,
                "mode": mode,
                "mixed": mixed,
                "sample": sample,
            }
            actual_mode = (
                "asset_batch" if mode in {"asset_only", "mixed_asset_declare"} else mode
            )
            result = _run_case(
                p, h, actual_mode, work / f"cell-{index:03d}", imports, mixed=mixed
            )
            result["case"] = mode
            result["sample"] = sample
            result["mixed"] = mixed
            results.append(result)
            timing_ms.append(result["timing_ms"])
            failed_cell = None
        if args.context_compat:
            phase = "exact_replay_probe"
            failed_cell = {
                "index": None,
                "P": 1,
                "H": 16,
                "mode": "separate_exact_replay_probe",
                "timing_pair": False,
            }
            replay_probe = _run_exact_replay_probe(work / "exact-replay-probe", imports)
            failed_cell = None
        grouped: dict[tuple[str, int, int], list[float]] = {}
        for item in results:
            grouped.setdefault((item["case"], item["P"], item["H"]), []).append(
                item["timing_ms"]
            )
        timing_summary = []
        for (case, p, h), samples in sorted(grouped.items()):
            ordered = sorted(samples)
            timing_summary.append(
                {
                    "case": case,
                    "P": p,
                    "H": h,
                    "sample_count": len(samples),
                    "median_ms": statistics.median(samples),
                    "p95_ms": _nearest_rank_p95(ordered),
                }
            )
        report = {
            "benchmark": "aigineering-asset-reducer-locality",
            "schema_version": 1,
            "source": {
                "source_label": args.source_revision,
                "requested_source_revision": args.source_revision,
                "imported_source_revision": None,
                "driver_checkout_revision": None,
                "package_root_mode": "explicit_package_root",
                "package_python_file_count": len(manifest),
                "package_manifest_sha256": package_manifest_sha,
                "package_python_files": manifest,
                "context_loader_api_capabilities": context_loader_api_capabilities,
                "context_loader_entry_path": context_loader_entry_path,
                "script_sha256": _sha(Path(__file__)),
                "python_version": platform.python_version(),
                "sqlite_version": sqlite3.sqlite_version,
            },
            "execution": {
                "accepted_publications": sum(
                    item["accepted_publications"] for item in results
                ),
                "signed_history_closed": all(
                    item["history"]["terminal"] >= item["history"]["contracts"]
                    for item in results
                ),
                "twin_semantics_equal": all(
                    item["twin_semantics_equal"] for item in results
                ),
                "case_count": len(results),
                "expected_timing_and_instrumentation_pairs": len(results),
                "correctness_microcases": microcases,
                "timing_execution_count": len(results),
                "instrumentation_execution_count": len(results),
                "source_mode": _mode_parameters(args)["mode"],
            },
            "replay_probe": replay_probe,
            "measurement": {
                "context_loader_calls": sum(
                    item["instrumentation"]["context_loader_calls"] for item in results
                ),
                "pre_commit_reducer_calls": sum(
                    item["instrumentation"]["pre_commit_reducer_calls"]
                    for item in results
                ),
                "writer_reducer_calls": sum(
                    item["instrumentation"]["writer_reducer_calls"] for item in results
                ),
                "cells": results,
                "timing_summary_by_cell": timing_summary,
                "timing_median_ms": statistics.median(timing_ms),
                "timing_p95_ms": _nearest_rank_p95(timing_ms),
                "sqlite_vm_proxy_note": "progress callbacks multiplied by interval; not exact opcodes, rows, a bound, or a scan count",
                "counter_relationship_notes": {
                    "context_rows": "full_projection_context_rows is a nested subset of context_rows; do not add them",
                    "writer_rows": "writer_ingress_consequences_total includes writer_reduce_asset_facts; do not add inclusive and leaf durations",
                    "store_rows": "stage_returned_rows is exclusive attribution by active wrapper stage; sum per Store method to compare with store_returned_rows",
                    "sqlite_rows": "sqlite_rows_fetched and stage_sql_rows_fetched count rows materialized by the SQLite row factory, including scalar/control query results; they are separate from Store API returns and domain decoders",
                    "decoder_rows": "stage_decoded_rows and decoded_rows count domain decoder calls, not SQL result rows",
                    "duration_stages": "stage_duration_ms contains wrapper durations; projection_context_total, pre_commit_reduce_asset_facts, and writer_ingress_consequences_total are parent-inclusive; writer_reduce_asset_facts is nested inside writer_ingress_consequences_total",
                    "vm_work": "callbacks multiplied by interval is a product proxy, not actual scanned rows",
                    "matrix": "timing_ms covers measured publications only; history_build_ms is separate; every listed cell has one instrumented twin",
                },
                "not_measured": [
                    "exact SQLite VM opcodes",
                    "rows visited without materialization by SQLite",
                    "uninstrumented native SQLite work",
                ],
                "parameters": {
                    **_mode_parameters(args),
                    "background_contracts_close_by": "signed worker claims and accepted output facts",
                    "A_measurement": "actual nonterminal Contracts after publication; unclosed is not equivalent to ready or claimable",
                },
            },
        }
        phase = "report_write"
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True, indent=2)
            handle.write("\n")
    except BaseException as exc:
        keep_work = work is not None
        failure = {
            "benchmark": "aigineering-asset-reducer-locality",
            "schema_version": 1,
            "failure_type": type(exc).__name__,
            "failure_phase": phase,
            "process_exit_code": 1,
            "failed_cell": failed_cell,
            "source": {
                "source_label": args.source_revision,
                "requested_source_revision": args.source_revision,
                "imported_source_revision": None,
                "package_root_mode": "explicit_package_root",
                "package_python_file_count": len(manifest),
                "package_manifest_sha256": package_manifest_sha,
                "package_python_files": manifest,
                "context_loader_api_capabilities": context_loader_api_capabilities,
                "context_loader_entry_path": context_loader_entry_path,
                "script_sha256": _sha(Path(__file__)),
                "python_version": platform.python_version(),
                "sqlite_version": sqlite3.sqlite_version,
            },
            "parameters": {
                **_mode_parameters(args),
                "source_revision_label": args.source_revision,
                "expected_package_manifest_sha256": args.expected_package_manifest_sha256,
            },
            "completed_cells": len(results),
            "completed_results": results,
            "private_diagnostics": {
                "temporary_work_retained": keep_work,
            },
        }
        try:
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(failure, handle, sort_keys=True)
                handle.write("\n")
        except FileExistsError:
            pass
        if keep_work:
            sidecar = args.output.with_name(
                args.output.name + ".private-diagnostics.json"
            )
            with sidecar.open("x", encoding="utf-8") as handle:
                json.dump(
                    {"temporary_work_location": str(work)},
                    handle,
                    sort_keys=True,
                )
                handle.write("\n")
        raise
    finally:
        import shutil

        if work is not None and not keep_work:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
