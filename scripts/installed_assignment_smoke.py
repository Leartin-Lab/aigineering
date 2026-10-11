"""Installed CLI acceptance for exact Worker assignments.

These are explicit fixture outputs, not model or independent-policy evidence.
Signing and execution use the installed harness/compiler and ordinary CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from installed_smoke import json_command
from installed_worker_coordination_smoke import (
    _assert_task_output,
    _CANDIDATE_SCRIPT,
    _IDENTITY_SCRIPT,
    _python_json,
    _task,
)

_CLAIM_SCRIPT = r"""
import sys
from pathlib import Path
from aigineering.agent.harness import HarnessCandidateAdapter, candidate_json
from aigineering.core.signing import Ed25519Signer
from aigineering.protocol.candidate import ActorKey
domain_id, worker_id, key_id, private_path, contract_id, request_id = sys.argv[1:]
signer = Ed25519Signer.from_private_key_hex(Path(private_path).read_text().strip())
key = ActorKey(worker_id, key_id, signer.kind, signer.signer_id, ("worker.submit",))
adapter = HarnessCandidateAdapter(domain_id, key, signer)
print(candidate_json(adapter.claim_candidate(
    request_id=request_id, contract_id=contract_id, lease_seconds=120,
)))
"""

_SNAPSHOT_SCRIPT = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
store = SQLiteStore.snapshot_from_path(sys.argv[1])
try:
    print(json.dumps({
        "schema_version": store.schema_version,
        "record_ids": [record.id for _, record in store.scan_runtime_records()],
        "digest": store.projection_index_digest(),
        "claims": {c.id: store.get_claim(c.id) for c in store.get_all_contracts()},
    }, sort_keys=True))
finally:
    store.close()
"""

_DIRECT_CLAIM_SCRIPT = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
store = SQLiteStore(sys.argv[1])
try:
    claim = store.claim_contract(sys.argv[2], sys.argv[3])
    print(json.dumps({"blocked": claim is None}))
finally:
    store.close()
"""

_REBUILD_SCRIPT = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
store = SQLiteStore(sys.argv[1])
try:
    before = [record.id for _, record in store.scan_runtime_records()]
    digest = store.projection_index_digest()
    store.rebuild_runtime_materializations()
    print(json.dumps({
        "same_ids": before == [r.id for _, r in store.scan_runtime_records()],
        "same_digest": digest == store.projection_index_digest(),
    }))
finally:
    store.close()
"""


_PROVENANCE_SCRIPT = r"""
import json, sys
from dataclasses import replace
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.runtime_record import validate_runtime_record
from aigineering.core.worker_routing import registration_from_record
store = SQLiteStore.snapshot_from_path(sys.argv[1])
try:
    expected = json.loads(sys.argv[2])
    records = tuple(record for _, record in store.scan_runtime_records())
    index = {record.id: record for record in records}
    facts = [r for r in records if r.record_type == "claim.assignment.changed"]
    expected_fields = {"actor_id", "candidate_id", "contract_id", "key_id",
                       "previous_assignment_id", "reason", "worker_id"}
    for fact in facts:
        validate_runtime_record(fact)
        assert set(fact.payload) == expected_fields
        wanted = expected[fact.id]
        for field in ("contract_id", "previous_assignment_id", "worker_id"):
            assert fact.payload[field] == wanted[field]
        parents = [index[parent] for parent in fact.causal_parents]
        receipts = [r for r in parents if r.record_type == "candidate.received"]
        assert len(receipts) == 1
        for field in ("actor_id", "candidate_id", "key_id"):
            assert receipts[0].payload[field] == fact.payload[field]
        declarations = [r for r in parents if r.record_type == "contract.declared"
                        and r.payload["contract"]["id"] == fact.payload["contract_id"]]
        assert len(declarations) == 1
        previous = fact.payload["previous_assignment_id"]
        if previous:
            assert previous in fact.causal_parents
            assert index[previous].record_type == "claim.assignment.changed"
            assert index[previous].payload["contract_id"] == fact.payload["contract_id"]
        worker = fact.payload["worker_id"]
        if worker:
            registrations = [r for r in parents if r.record_type == "worker.registered"
                             and r.payload["worker_id"] == worker]
            assert len(registrations) == 1
            current = store.get_worker_registration(worker)
            assert registration_from_record(registrations[0]) == replace(
                current, active_claims=0)
        heads = [r for r in records if r.record_type == "candidate.committed"
                 and r.payload["candidate_id"] == fact.payload["candidate_id"]]
        assert len(heads) == 1
        assert fact.id in heads[0].payload["committed_record_ids"]
        assert receipts[0].id in heads[0].causal_parents
        source_id = wanted.get("source_contract_id")
        if source_id:
            source_head = wanted["source_assignment_id"]
            source_history = [r for r in facts if r.payload["contract_id"] == source_id]
            if source_head:
                assert source_head in fact.causal_parents
                source_fact = index[source_head]
                assert source_fact.record_type == "claim.assignment.changed"
                assert source_fact.payload["contract_id"] == source_id
                assert source_history[-1].id == source_head
            else:
                assert not source_history
                assert "" not in fact.causal_parents
            link = index[wanted["link_id"]]
            assert link.record_type == "task.handed_off"
            assert link.payload["contract_id"] == source_id
            assert link.payload["successor_id"] == fact.payload["contract_id"]
            assert link.payload["candidate_id"] == fact.payload["candidate_id"]
            assert link.payload["source_assignment_id"] == source_head
            assert link.payload["assignment_fact_id"] == fact.id
            assert link.payload["assignment_mode"] == wanted["assignment_mode"]
            assert link.payload["worker_id"] == fact.payload["worker_id"]
            assert fact.id in link.causal_parents
            terminals = [index[parent] for parent in link.causal_parents
                         if index[parent].record_type == "lifecycle.terminal"
                         and index[parent].payload["contract_id"] == source_id]
            assert len(terminals) == 1
            assert terminals[0].id in heads[0].payload["committed_record_ids"]
            assert terminals[0].payload["terminal"] == "cancelled"
    assert len(facts) == len(expected) == 8
    print(json.dumps({"assignment_facts": len(facts), "exact_parents_checked": True}))
finally:
    store.close()
"""


def task_assignment_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Exercise assignment, clear, signed claims, atomic handoff and replay."""
    workspace = cwd / "installed-task-assignments"
    workspace.mkdir()
    database = workspace / ".aig" / "store.db"
    local_env = dict(env)
    local_env.pop("PYTHONPATH", None)
    local_env["AIG_ACTOR_KEY_FILE"] = str(
        workspace / ".aig" / "identity" / "root.ed25519"
    )

    def command(*args: str) -> dict:
        result = json_command(
            aig, ["-C", str(workspace), *args], cwd=cwd, env=local_env
        )
        if args[:2] in {
            ("task", "assign"),
            ("task", "reassign"),
            ("task", "assignments"),
            ("task", "handoff"),
        } and (
            result.get("ok") is not True
            or not isinstance(result.get("data"), dict)
            or type(result.get("revision")) is not int
        ):
            raise RuntimeError("assignment CLI returned an invalid success envelope")
        return result

    def snapshot() -> dict:
        return _python_json(
            python, _SNAPSHOT_SCRIPT, [str(database)], cwd=cwd, env=local_env
        )

    def rejected(*args: str) -> None:
        before = snapshot()["record_ids"]
        result = subprocess.run(
            [str(aig), "-C", str(workspace), *args, "--json"],
            cwd=cwd,
            env=local_env,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            raise RuntimeError(f"assignment preflight accepted invalid {args[1]}")
        payload = json.loads(result.stdout)
        if payload.get("ok") is not False or snapshot()["record_ids"] != before:
            raise RuntimeError("invalid assignment changed facts or lacked JSON error")

    initialized = command("domain", "init")
    domain_id = initialized.get("domain_id") or command("domain", "show")["domain_id"]
    identities: dict[str, tuple[str, Path]] = {}
    for worker_id in ("assignment-worker-a", "assignment-worker-b"):
        key_id = worker_id + "-key"
        directory = workspace / (".private-" + worker_id)
        metadata = _python_json(
            python,
            _IDENTITY_SCRIPT,
            [str(directory), worker_id, key_id],
            cwd=cwd,
            env=local_env,
        )
        identities[worker_id] = (key_id, directory / "harness.ed25519")
        command(
            "worker",
            "register",
            "--worker-id",
            worker_id,
            "--key-id",
            key_id,
            "--public-key",
            metadata["public_key"],
            "--signature-kind",
            metadata["signature_kind"],
            "--capacity",
            "2",
        )
    worker_a, worker_b = tuple(identities)

    def claim(worker_id: str, target: str, request: str) -> dict:
        key_id, private_path = identities[worker_id]
        signed = _python_json(
            python,
            _CLAIM_SCRIPT,
            [domain_id, worker_id, key_id, str(private_path), target, request],
            cwd=cwd,
            env=local_env,
        )
        package = command("worker", "next", "--candidate", json.dumps(signed))
        if package is None:
            return {}
        if not isinstance(package, dict):
            raise RuntimeError("installed claim returned an invalid package")
        return package

    def finish(worker_id: str, package: dict, output_name: str) -> None:
        target = package["contract_id"]
        package_path = workspace / (package["claim_id"].rsplit(":", 1)[-1] + ".json")
        package_path.write_text(json.dumps(package), encoding="utf-8")
        key_id, private_path = identities[worker_id]
        value = "installed-assignment-fixture:" + output_name
        signed = _python_json(
            python,
            _CANDIDATE_SCRIPT,
            [
                domain_id,
                worker_id,
                key_id,
                str(private_path),
                "result",
                "assignment-result:" + target,
                str(package_path),
                "/exec " + json.dumps({"outputs": {output_name: value}}),
            ],
            cwd=cwd,
            env=local_env,
        )
        submitted = json.loads(
            subprocess.check_output(
                [
                    str(aig),
                    "-C",
                    str(workspace),
                    "worker",
                    "submit",
                    "--json",
                    json.dumps(signed),
                ],
                cwd=cwd,
                env=local_env,
                text=True,
            )
        )
        if submitted.get("status") != "accepted":
            raise RuntimeError("assigned Worker fixture result was not accepted")
        _assert_task_output(
            aig,
            workspace,
            target,
            output_name,
            value,
            workspace / (output_name + ".txt"),
            cwd=cwd,
            env=local_env,
        )

    target = _task(
        aig, workspace, "assignment-inherit", "inherit-result", cwd=cwd, env=local_env
    )
    rejected("task", "assign", target, "unknown-worker", "--reason", "Invalid target.")
    assign_args = (
        "task",
        "assign",
        target,
        worker_a,
        "--reason",
        "Initial exact Worker.",
        "--idempotency-key",
        "installed-assignment-initial",
    )
    initial = command(*assign_args)["data"]
    replace_args = (
        "task",
        "reassign",
        target,
        "--worker",
        worker_b,
        "--previous",
        initial["assignment_id"],
        "--reason",
        "Replace before claim.",
        "--idempotency-key",
        "installed-assignment-replace",
    )
    replaced = command(*replace_args)["data"]
    clear_args = (
        "task",
        "reassign",
        target,
        "--clear",
        "--previous",
        replaced["assignment_id"],
        "--reason",
        "Clear before claim.",
        "--idempotency-key",
        "installed-assignment-clear",
    )
    cleared = command(*clear_args)["data"]
    if cleared["worker_id"] != "":
        raise RuntimeError("clear did not retain an explicit empty Worker head")
    rejected(
        "task",
        "reassign",
        target,
        "--clear",
        "--previous",
        cleared["assignment_id"],
        "--reason",
        "Redundant clear.",
    )
    current = command(
        "task",
        "reassign",
        target,
        "--worker",
        worker_a,
        "--previous",
        cleared["assignment_id"],
        "--reason",
        "Set after clear.",
    )["data"]
    before_wrong_claim = snapshot()["record_ids"]
    wrong = claim(worker_b, target, "assignment-wrong-worker")
    if wrong.get("claim_id") or snapshot()["record_ids"] != before_wrong_claim:
        raise RuntimeError("wrong signed Worker acquired work or wrote claim facts")
    direct = _python_json(
        python,
        _DIRECT_CLAIM_SCRIPT,
        [str(database), target, worker_b],
        cwd=cwd,
        env=local_env,
    )
    if direct != {"blocked": True}:
        raise RuntimeError("trusted Store claim bypassed assignment")
    original_package = claim(worker_a, target, "assignment-correct-worker")
    if original_package.get("contract_id") != target:
        raise RuntimeError("assigned Worker could not claim its exact task")
    rejected(
        "task",
        "reassign",
        target,
        "--worker",
        worker_b,
        "--previous",
        current["assignment_id"],
        "--reason",
        "Cannot reset a claim.",
    )

    handoff_args = (
        "task",
        "handoff",
        target,
        "--reason",
        "Continue the same obligation.",
        "--idempotency-key",
        "installed-assignment-handoff",
    )
    handoff = command(*handoff_args)["data"]
    successor = handoff["successor_id"]
    inherited = command("task", "assignments", successor)["data"]
    if inherited["worker_id"] != worker_a:
        raise RuntimeError("handoff did not inherit assignment atomically")
    old_claim = snapshot()["claims"][target]
    if (
        old_claim["status"] != "released"
        or old_claim["claim_id"] != original_package["claim_id"]
    ):
        raise RuntimeError("handoff transferred or reset the original claim")
    if claim(worker_b, successor, "assignment-wrong-successor").get("claim_id"):
        raise RuntimeError("handoff successor was exposed to the wrong Worker")
    successor_package = claim(worker_a, successor, "assignment-right-successor")
    finish(worker_a, successor_package, "inherit-result")
    before_replay = snapshot()["record_ids"]
    if command(*assign_args)["data"]["assignment_id"] != initial["assignment_id"]:
        raise RuntimeError("initial assignment replay changed its fact")
    for args, expected in ((replace_args, replaced), (clear_args, cleared)):
        if command(*args)["data"]["assignment_id"] != expected["assignment_id"]:
            raise RuntimeError("reassignment replay changed its original fact")
    if command(*handoff_args)["data"]["successor_id"] != successor:
        raise RuntimeError("handoff replay changed its successor")
    if snapshot()["record_ids"] != before_replay:
        raise RuntimeError("exact replay appended facts after successor completion")

    clear_source = _task(
        aig,
        workspace,
        "assignment-override-clear",
        "clear-result",
        cwd=cwd,
        env=local_env,
    )
    clear_assignment = command(
        "task",
        "assign",
        clear_source,
        worker_a,
        "--reason",
        "Assign before override.",
    )["data"]
    clear_head = clear_assignment["assignment_id"]
    clear_handoff = command(
        "task",
        "handoff",
        clear_source,
        "--clear-assignment",
        "--source-assignment-id",
        clear_head,
        "--reason",
        "Explicitly clear successor.",
    )["data"]
    clear_successor = clear_handoff["successor_id"]
    clear_history = command("task", "assignments", clear_successor)["data"]
    if (
        not clear_history["head_assignment_id"]
        or clear_history["worker_id"] is not None
    ):
        raise RuntimeError("override-clear omitted its cleared successor fact")
    finish(
        worker_b,
        claim(worker_b, clear_successor, "assignment-clear-successor"),
        "clear-result",
    )
    fresh_source = _task(
        aig,
        workspace,
        "assignment-override-first",
        "first-result",
        cwd=cwd,
        env=local_env,
    )
    fresh_handoff_args = (
        "task",
        "handoff",
        fresh_source,
        "--assignment-worker",
        worker_b,
        "--source-assignment-id",
        "",
        "--reason",
        "Select a Worker during successor creation.",
        "--idempotency-key",
        "installed-assignment-first-override",
    )
    fresh_handoff = command(*fresh_handoff_args)["data"]
    fresh_successor = fresh_handoff["successor_id"]
    fresh_history = command("task", "assignments", fresh_successor)["data"]
    if (
        not fresh_history["head_assignment_id"]
        or fresh_history["worker_id"] != worker_b
    ):
        raise RuntimeError("no-history override did not select its exact Worker")
    if claim(worker_a, fresh_successor, "assignment-wrong-first-override").get(
        "claim_id"
    ):
        raise RuntimeError(
            "no-history override exposed the successor to another Worker"
        )
    finish(
        worker_b,
        claim(worker_b, fresh_successor, "assignment-right-first-override"),
        "first-result",
    )
    before_fresh_replay = snapshot()["record_ids"]
    if command(*fresh_handoff_args)["data"]["successor_id"] != fresh_successor:
        raise RuntimeError("no-history override replay changed its successor")
    if snapshot()["record_ids"] != before_fresh_replay:
        raise RuntimeError("no-history override replay appended facts after completion")
    events_before = command("task", "events", successor)["data"]
    if not any(
        isinstance(item.get("worker_ids"), list) and worker_a in item["worker_ids"]
        for item in events_before
    ):
        raise RuntimeError("Redis feed omitted typed assignment Worker references")
    histories_before = command("task", "assignments", successor)["data"]
    rebuilt = _python_json(
        python, _REBUILD_SCRIPT, [str(database)], cwd=cwd, env=local_env
    )
    if rebuilt != {"same_ids": True, "same_digest": True}:
        raise RuntimeError("assignment materialization rebuild changed facts or digest")
    command("redis", "rebuild")
    if command("task", "events", successor)["data"] != events_before:
        raise RuntimeError("Redis rebuild changed assignment event references")
    if command("task", "assignments", successor)["data"] != histories_before:
        raise RuntimeError("reopen/rebuild changed assignment history")
    if snapshot()["schema_version"] != 21:
        raise RuntimeError("installed assignment is missing its schema21 reader gate")
    expected_facts = {
        item["assignment_id"]: {
            field: item[field]
            for field in ("contract_id", "previous_assignment_id", "worker_id")
        }
        for item in (initial, replaced, cleared, current, clear_assignment)
    }
    for history, source_id, source_head, link, selected, mode in (
        (inherited, target, current["assignment_id"], handoff, worker_a, "inherit"),
        (clear_history, clear_source, clear_head, clear_handoff, "", "override"),
        (fresh_history, fresh_source, "", fresh_handoff, worker_b, "override"),
    ):
        expected_facts[history["head_assignment_id"]] = {
            "contract_id": link["successor_id"],
            "previous_assignment_id": "",
            "worker_id": selected,
            "source_contract_id": source_id,
            "source_assignment_id": source_head,
            "link_id": link["record_id"],
            "assignment_mode": mode,
        }
    provenance = _python_json(
        python,
        _PROVENANCE_SCRIPT,
        [str(database), json.dumps(expected_facts)],
        cwd=cwd,
        env=local_env,
    )
    return {
        "classification": "installed_explicit_fixture",
        "provider_calls": 0,
        "schema_version": 21,
        "set_replace_clear_set": True,
        "invalid_and_claimed_preflight_no_facts": True,
        "signed_and_trusted_wrong_worker_claims_blocked": True,
        "claimed_source_handoff_inherits_atomically": True,
        "old_claim_released_without_reset_or_transfer": True,
        "authorized_override_clear_has_successor_head": True,
        "authorized_override_set_without_source_history": True,
        "exact_assignment_and_handoff_replay_after_terminal": True,
        "exact_replace_and_clear_replay_after_terminal": True,
        "normal_signed_fixture_outputs_export_exact": True,
        "sqlite_and_redis_rebuild_equal": True,
        "assignment_fact_provenance": provenance,
        "target_contract_id": target,
        "successor_contract_id": successor,
        "clear_successor_contract_id": clear_successor,
        "first_override_successor_contract_id": fresh_successor,
    }
