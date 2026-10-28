"""Installed-wheel checks for signed bulk task lifecycle controls.

The fixture uses the installed CLI and signed local operators only. It does
not invoke a provider or publish business output. Raw command streams stay in
the private smoke workspace; the returned value contains identifiers and
phase summaries only.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

_EVIDENCE_SCRIPT = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.immutability import deep_thaw

store = SQLiteStore.snapshot_from_path(sys.argv[1])
try:
    rows = store.scan_runtime_records()
    records = [record for _, record in rows]
    print(json.dumps({
        "record_ids": [record.id for record in records],
        "digest": store.projection_index_digest(),
        "records": [{"record_id": record.id, "record_type": record.record_type,
                     "payload": deep_thaw(record.payload),
                     "causal_parents": list(record.causal_parents),
                     "recorded_at": record.recorded_at}
                    for record in records
                    if record.record_type in {
                        "candidate.received", "candidate.committed",
                        "claim.hold.placed", "claim.hold.released", "task.promoted",
                        "lifecycle.terminal",
                    }],
        "claims": {contract.id: store.get_claim(contract.id)
                   for contract in store.get_all_contracts()},
    }, sort_keys=True))
finally:
    store.close()
"""

_REOPEN_REBUILD_SCRIPT = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore

path = sys.argv[1]
first = SQLiteStore(path)
try:
    records_before = [(record.id, record.recorded_at)
                      for _, record in first.scan_runtime_records()]
    ids_before = [record_id for record_id, _ in records_before]
    digest_before = first.projection_index_digest()
finally:
    first.close()
second = SQLiteStore(path)
try:
    records_reopened = [(record.id, record.recorded_at)
                        for _, record in second.scan_runtime_records()]
    ids_reopened = [record_id for record_id, _ in records_reopened]
    digest_reopened = second.projection_index_digest()
    second.rebuild_runtime_materializations()
    records_rebuilt = [(record.id, record.recorded_at)
                       for _, record in second.scan_runtime_records()]
    ids_rebuilt = [record_id for record_id, _ in records_rebuilt]
    digest_rebuilt = second.projection_index_digest()
finally:
    second.close()
third = SQLiteStore(path)
try:
    records_final = [(record.id, record.recorded_at)
                     for _, record in third.scan_runtime_records()]
    ids_final = [record_id for record_id, _ in records_final]
    digest_final = third.projection_index_digest()
finally:
    third.close()
print(json.dumps({
    "reopen_same_ids": ids_before == ids_reopened,
    "reopen_same_digest": digest_before == digest_reopened,
    "rebuild_same_ids": ids_before == ids_rebuilt == ids_final,
    "rebuild_same_digest": digest_before == digest_rebuilt == digest_final,
    "reopen_same_timestamps": records_before == records_reopened,
    "rebuild_same_timestamps": records_before == records_rebuilt == records_final,
    "record_count": len(ids_before),
    "digest": digest_before,
}, sort_keys=True))
"""


def _private_write(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def _private_append_json(path: Path, payload: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")


def installed_task_bulk_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Exercise bulk block, unblock, promote, and cancel on an installed wheel."""
    workspace = cwd / "installed-task-bulk"
    workspace.mkdir(mode=0o700)
    logs = workspace / "private-command-streams"
    logs.mkdir(mode=0o700)
    command_manifest = logs / "command-summaries.jsonl"
    database = workspace / ".aig" / "store.db"
    local_env = dict(env)
    local_env.pop("PYTHONPATH", None)
    local_env["AIG_ACTOR_KEY_FILE"] = str(
        workspace / ".aig" / "identity" / "root.ed25519"
    )
    redis_url = local_env.get("AIGINEERING_REDIS_URL", "")
    if not redis_url.strip():
        raise RuntimeError(
            "bulk installed control requires an explicitly configured Redis URL"
        )

    command_index = 0
    command_summaries: list[dict] = []

    def invoke(*args: str) -> subprocess.CompletedProcess[str]:
        nonlocal command_index
        command_index += 1
        full_command = [str(aig), "-C", str(workspace), *args, "--json"]
        completed = subprocess.run(
            full_command,
            cwd=cwd,
            env=local_env,
            check=False,
            text=True,
            capture_output=True,
        )
        prefix = f"{command_index:04d}"
        stdout_path = logs / f"{prefix}-stdout.txt"
        stderr_path = logs / f"{prefix}-stderr.txt"
        _private_write(stdout_path, completed.stdout)
        _private_write(stderr_path, completed.stderr)
        summary = {
            "phase": args[1] if len(args) > 1 else "unknown",
            "exit_code": completed.returncode,
            "stdout_sha256": hashlib.sha256(
                completed.stdout.encode("utf-8")
            ).hexdigest(),
            "stderr_sha256": hashlib.sha256(
                completed.stderr.encode("utf-8")
            ).hexdigest(),
            "stdout_bytes": len(completed.stdout.encode("utf-8")),
            "stderr_bytes": len(completed.stderr.encode("utf-8")),
        }
        command_summaries.append(summary)
        _private_append_json(command_manifest, summary)
        return completed

    def inspect_python(phase: str, script: str, *args: str) -> str:
        nonlocal command_index
        command_index += 1
        completed = subprocess.run(
            [str(python.resolve()), "-c", script, *args],
            cwd=cwd,
            env=local_env,
            check=False,
            text=True,
            capture_output=True,
        )
        prefix = f"{command_index:04d}"
        stdout_path = logs / f"{prefix}-stdout.txt"
        stderr_path = logs / f"{prefix}-stderr.txt"
        _private_write(stdout_path, completed.stdout)
        _private_write(stderr_path, completed.stderr)
        summary = {
            "phase": phase,
            "exit_code": completed.returncode,
            "stdout_sha256": hashlib.sha256(
                completed.stdout.encode("utf-8")
            ).hexdigest(),
            "stderr_sha256": hashlib.sha256(
                completed.stderr.encode("utf-8")
            ).hexdigest(),
            "stdout_bytes": len(completed.stdout.encode("utf-8")),
            "stderr_bytes": len(completed.stderr.encode("utf-8")),
        }
        command_summaries.append(summary)
        _private_append_json(command_manifest, summary)
        if completed.returncode != 0:
            raise RuntimeError(
                f"private installed inspection failed in phase {phase} "
                f"(exit={completed.returncode}; private command #{command_index:04d})"
            )
        return completed.stdout

    def success_json_value(*args: str):
        completed = invoke(*args)
        if completed.returncode != 0:
            raise RuntimeError(
                f"installed bulk command rejected in phase {args[1:2]} "
                f"(exit={completed.returncode}; private command #{command_index:04d})"
            )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"installed bulk command returned invalid JSON "
                f"(private command #{command_index:04d})"
            ) from exc
        return result

    def accepted(*args: str) -> dict:
        result = success_json_value(*args)
        if not isinstance(result, dict):
            raise RuntimeError("installed bulk command returned a non-object")
        return result

    def failure_json(*args: str) -> dict:
        completed = invoke(*args)
        if completed.returncode == 0:
            raise RuntimeError(
                f"installed command unexpectedly succeeded in phase {args[1:2]}"
            )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"installed failure returned invalid JSON "
                f"(private command #{command_index:04d})"
            ) from exc
        if not isinstance(result, dict):
            raise RuntimeError("installed failure returned a non-object")
        return result

    def rejected_without_facts(phase: str, *args: str) -> None:
        before = evidence()["record_ids"]
        completed = invoke(*args)
        if completed.returncode == 0:
            raise RuntimeError(f"invalid bulk shape was accepted in phase {phase}")
        after = evidence()["record_ids"]
        if after != before:
            raise RuntimeError(f"invalid bulk shape wrote facts in phase {phase}")

    def evidence() -> dict:
        result = json.loads(
            inspect_python("sqlite-facts", _EVIDENCE_SCRIPT, str(database))
        )
        if not isinstance(result.get("record_ids"), list):
            raise RuntimeError("bulk fact inspection returned invalid record IDs")
        return result

    def create_task(name: str, output: str) -> str:
        result = accepted(
            "task",
            "create",
            "--name",
            name,
            "--description",
            f"Installed signed bulk control fixture: {name}.",
            "--budget",
            "3",
            "--output",
            output,
        )
        contract_id = result.get("contract_id")
        if not isinstance(contract_id, str) or not contract_id:
            raise RuntimeError("installed task creation omitted its Contract ID")
        return contract_id

    def derived_key(action: str, base: str, contract_id: str) -> str:
        canonical = json.dumps(
            ("task-bulk-v1", action, base, contract_id),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8", errors="strict")).hexdigest()
        return f"task-bulk-v1:{action}:{digest}"

    def result_rows(
        envelope: dict, targets: tuple[str, ...], *, expected_ok: int
    ) -> list[dict]:
        data = envelope.get("data")
        rows = data.get("results") if isinstance(data, dict) else None
        if (
            not isinstance(rows, list)
            or [row.get("contract_id") for row in rows] != list(targets)
            or data.get("success_count") != expected_ok
            or data.get("failed_count") != len(targets) - expected_ok
            or data.get("pending_count") != 0
            or data.get("unknown_count") != 0
            or data.get("not_attempted_count") != 0
            or type(envelope.get("revision")) is not int
        ):
            raise RuntimeError("bulk result envelope did not preserve target outcomes")
        if (envelope.get("ok") is True) != (expected_ok == len(targets)):
            raise RuntimeError("bulk result envelope ok flag hid a partial failure")
        return rows

    def verify_candidate_rows(rows: list[dict], expected: dict[str, str]) -> None:
        inspected = evidence()
        by_id = {record["record_id"]: record for record in inspected["records"]}
        received = [
            row
            for row in inspected["records"]
            if row["record_type"] == "candidate.received"
        ]
        heads = [
            row
            for row in inspected["records"]
            if row["record_type"] == "candidate.committed"
        ]
        for row in rows:
            if not row.get("ok"):
                continue
            candidate_id = row.get("candidate_id")
            fact_ids = row.get("fact_ids")
            key = row.get("idempotency_key")
            contract_id = row.get("contract_id")
            if (
                not isinstance(candidate_id, str)
                or not candidate_id
                or not isinstance(key, str)
                or not key
                or not isinstance(fact_ids, list)
                or not fact_ids
                or expected.get(contract_id) != key
            ):
                raise RuntimeError("accepted target omitted its exact key or fact IDs")
            matching_receipts = [
                item
                for item in received
                if item["payload"].get("candidate_id") == candidate_id
            ]
            matching_heads = [
                item
                for item in heads
                if item["payload"].get("candidate_id") == candidate_id
            ]
            if len(matching_receipts) != 1 or len(matching_heads) != 1:
                raise RuntimeError("accepted target lacks one receipt and exact head")
            receipt = matching_receipts[0]
            head = matching_heads[0]
            members = head["payload"].get("committed_record_ids", [])
            target_facts = [by_id[fact_id] for fact_id in fact_ids if fact_id in by_id]
            if (
                set(fact_ids) != set(members)
                or head["causal_parents"] != [receipt["record_id"], *members]
                or any(fact_id not in by_id for fact_id in fact_ids)
                or len(target_facts) != len(fact_ids)
                or any(
                    item["payload"].get("contract_id") != contract_id
                    for item in target_facts
                )
            ):
                raise RuntimeError(
                    "accepted target fact/receipt/head membership differs"
                )

    accepted("domain", "init")
    target_a = create_task("bulk-block-a", "bulk-block-a-output")
    target_claimed = create_task("bulk-block-claimed", "bulk-block-claimed-output")
    target_c = create_task("bulk-block-c", "bulk-block-c-output")
    claimed_package = accepted(
        "worker",
        "next",
        "--worker-id",
        "installed-bulk-claim-worker",
        "--contract",
        target_claimed,
    )
    if claimed_package.get("contract_id") != target_claimed or not claimed_package.get(
        "claim_id"
    ):
        raise RuntimeError("fixture failed to establish a live signed task claim")

    block_targets = (target_a, "task:unknown-bulk-target", target_claimed, target_c)
    block_base = "installed-task-bulk-block-v1"
    block_args = (
        "task",
        "block",
        *block_targets,
        "--reason",
        "Installed bulk hold fixture.",
        "--idempotency-key",
        block_base,
    )
    block = failure_json(*block_args)
    block_rows = result_rows(block, block_targets, expected_ok=2)
    if [row.get("ok") for row in block_rows] != [True, False, False, True]:
        raise RuntimeError("bulk block did not retain middle unknown/claimed failures")
    if [row.get("outcome") for row in block_rows if not row.get("ok")] != [
        "not_found",
        "rejected",
    ]:
        raise RuntimeError("bulk block failures were not classified per target")
    block_data = {
        row["contract_id"]: row["data"] for row in block_rows if row.get("ok")
    }
    block_keys = {
        row["contract_id"]: row["idempotency_key"]
        for row in block_rows
        if row.get("ok")
    }
    if block_keys != {
        contract_id: derived_key("block", block_base, contract_id)
        for contract_id in (target_a, target_c)
    }:
        raise RuntimeError("bulk block did not return canonical per-target keys")
    if block.get("data", {}).get("batch_idempotency_key") != block_base:
        raise RuntimeError("explicit block batch key was not returned unchanged")
    verify_candidate_rows(block_rows, block_keys)
    hold_ids = {
        contract_id: data.get("hold_id") or data.get("record_id")
        for contract_id, data in block_data.items()
    }
    if any(not value for value in hold_ids.values()):
        raise RuntimeError("bulk block result omitted a durable hold ID")

    # New batch keys are independent of input order and sibling subsets.
    replay_subset = accepted(
        "task",
        "block",
        target_c,
        target_a,
        "--reason",
        "Installed bulk hold fixture.",
        "--idempotency-key",
        block_base,
    )
    subset_rows = result_rows(replay_subset, (target_c, target_a), expected_ok=2)
    if any(
        row.get("candidate_id")
        != next(
            item["candidate_id"]
            for item in block_rows
            if item["contract_id"] == row["contract_id"]
        )
        for row in subset_rows
    ):
        raise RuntimeError("block key derivation changed under reordered subset replay")
    verify_candidate_rows(subset_rows, block_keys)

    before_invalid = evidence()["record_ids"]
    rejected_without_facts(
        "duplicate-target",
        "task",
        "block",
        target_a,
        target_a,
        "--reason",
        "duplicate fixture",
    )
    rejected_without_facts(
        "blank-target", "task", "block", "", "--reason", "blank fixture"
    )
    rejected_without_facts("blank-reason", "task", "block", target_a, "--reason", "")
    rejected_without_facts(
        "blank-base-key",
        "task",
        "block",
        target_a,
        "--reason",
        "shape fixture",
        "--idempotency-key",
        "",
    )
    too_many = tuple(f"task:shape-{index:02d}" for index in range(65))
    rejected_without_facts(
        "sixty-five-targets", "task", "block", *too_many, "--reason", "limit fixture"
    )
    if evidence()["record_ids"] != before_invalid:
        raise RuntimeError("global shape rejection changed SQLite facts")

    unblock_targets = (target_a, target_c)
    rejected_without_facts(
        "unblock-wrong-target",
        "task",
        "unblock",
        *unblock_targets,
        "--reason",
        "mapping fixture",
        "--hold-for",
        f"{target_a}={hold_ids[target_c]}",
        "--hold-for",
        f"{target_c}={hold_ids[target_a]}",
    )
    rejected_without_facts(
        "unblock-incomplete-map",
        "task",
        "unblock",
        *unblock_targets,
        "--reason",
        "mapping fixture",
        "--hold-for",
        f"{target_a}={hold_ids[target_a]}",
    )
    rejected_without_facts(
        "unblock-duplicate-map",
        "task",
        "unblock",
        *unblock_targets,
        "--reason",
        "mapping fixture",
        "--hold-for",
        f"{target_a}={hold_ids[target_a]}",
        "--hold-for",
        f"{target_a}={hold_ids[target_a]}",
        "--hold-for",
        f"{target_c}={hold_ids[target_c]}",
    )
    rejected_without_facts(
        "unblock-blank-map",
        "task",
        "unblock",
        *unblock_targets,
        "--reason",
        "mapping fixture",
        "--hold-for",
        f"={hold_ids[target_a]}",
        "--hold-for",
        f"{target_c}={hold_ids[target_c]}",
    )
    rejected_without_facts(
        "unblock-mixed-hold-syntax",
        "task",
        "unblock",
        *unblock_targets,
        "--reason",
        "mapping fixture",
        "--hold",
        hold_ids[target_a],
        "--hold-for",
        f"{target_c}={hold_ids[target_c]}",
    )
    unblock = accepted(
        "task",
        "unblock",
        *unblock_targets,
        "--reason",
        "Release exact installed bulk holds.",
        "--hold-for",
        f"{target_a}={hold_ids[target_a]}",
        "--hold-for",
        f"{target_c}={hold_ids[target_c]}",
        "--idempotency-key",
        "installed-task-bulk-unblock-v1",
    )
    unblock_rows = result_rows(unblock, unblock_targets, expected_ok=2)
    unblock_keys = {row["contract_id"]: row["idempotency_key"] for row in unblock_rows}
    if unblock_keys != {
        contract_id: derived_key(
            "unblock", "installed-task-bulk-unblock-v1", contract_id
        )
        for contract_id in unblock_targets
    }:
        raise RuntimeError("bulk unblock did not return canonical per-target keys")
    verify_candidate_rows(unblock_rows, unblock_keys)
    unblock_base = "installed-task-bulk-unblock-v1"
    if unblock.get("data", {}).get("batch_idempotency_key") != unblock_base:
        raise RuntimeError("explicit unblock batch key was not returned unchanged")
    before_unblock_replay = evidence()
    unblock_replay = accepted(
        "task",
        "unblock",
        target_c,
        target_a,
        "--reason",
        "Release exact installed bulk holds.",
        "--hold-for",
        f"{target_c}={hold_ids[target_c]}",
        "--hold-for",
        f"{target_a}={hold_ids[target_a]}",
        "--idempotency-key",
        unblock_base,
    )
    unblock_replay_rows = result_rows(
        unblock_replay, (target_c, target_a), expected_ok=2
    )
    for row in unblock_replay_rows:
        original = next(
            item for item in unblock_rows if item["contract_id"] == row["contract_id"]
        )
        if (
            row.get("idempotency_key") != original.get("idempotency_key")
            or row.get("candidate_id") != original.get("candidate_id")
            or row.get("fact_ids") != original.get("fact_ids")
        ):
            raise RuntimeError(
                "reordered unblock replay changed its original Candidate"
            )
    if evidence()["record_ids"] != before_unblock_replay["record_ids"]:
        raise RuntimeError("unblock replay appended facts")
    single_unblock_replay = accepted(
        "task",
        "unblock",
        target_a,
        "--reason",
        "Release exact installed bulk holds.",
        "--hold",
        hold_ids[target_a],
        "--idempotency-key",
        unblock_keys[target_a],
    )
    original_unblock_a = next(
        item for item in unblock_rows if item["contract_id"] == target_a
    )
    if (
        single_unblock_replay.get("data", {}).get("record_id")
        != original_unblock_a.get("data", {}).get("record_id")
        or single_unblock_replay.get("data", {}).get("candidate_id")
        != original_unblock_a.get("candidate_id")
        or single_unblock_replay.get("data", {}).get("fact_ids")
        != original_unblock_a.get("fact_ids")
    ):
        raise RuntimeError("one-target unblock replay by returned key changed facts")
    if evidence()["record_ids"] != before_unblock_replay["record_ids"]:
        raise RuntimeError("one-target unblock replay appended facts")
    for contract_id in unblock_targets:
        status = accepted("task", "status", contract_id)
        if status.get("status") == "blocked":
            raise RuntimeError("exact bulk unblock left an original hold active")

    # Replaying the original block after release returns its old facts only.
    before_block_replay = evidence()["record_ids"]
    block_after_release = failure_json(*block_args)
    after_rows = result_rows(block_after_release, block_targets, expected_ok=2)
    if evidence()["record_ids"] != before_block_replay:
        raise RuntimeError(
            "block replay after release appended or recreated hold facts"
        )
    single_block_replay = accepted(
        "task",
        "block",
        target_a,
        "--reason",
        "Installed bulk hold fixture.",
        "--idempotency-key",
        block_keys[target_a],
    )
    original_block_a = next(
        item for item in block_rows if item["contract_id"] == target_a
    )
    if (
        single_block_replay.get("data", {}).get("record_id")
        != original_block_a.get("data", {}).get("record_id")
        or single_block_replay.get("data", {}).get("candidate_id")
        != original_block_a.get("candidate_id")
        or single_block_replay.get("data", {}).get("fact_ids")
        != original_block_a.get("fact_ids")
    ):
        raise RuntimeError("one-target block replay by returned key changed facts")
    if evidence()["record_ids"] != before_block_replay:
        raise RuntimeError("one-target block replay recreated a released hold")
    for row in after_rows:
        if row.get("ok"):
            prior = next(
                item for item in block_rows if item["contract_id"] == row["contract_id"]
            )
            if row.get("candidate_id") != prior.get("candidate_id") or row.get(
                "fact_ids"
            ) != prior.get("fact_ids"):
                raise RuntimeError(
                    "released hold replay changed its original Candidate"
                )
    verify_candidate_rows(after_rows, block_keys)

    # Legacy promote key derivation remains `<base>:promote:<target>`.
    promote_a = create_task("bulk-promote-a", "bulk-promote-a-output")
    promote_b = create_task("bulk-promote-b", "bulk-promote-b-output")
    p_a_hold_1 = (
        accepted("task", "block", promote_a, "--reason", "First promotion hold.")
        .get("data", {})
        .get("hold_id")
    )
    p_a_hold_2 = (
        accepted("task", "block", promote_a, "--reason", "Second promotion hold.")
        .get("data", {})
        .get("hold_id")
    )
    p_b_hold = (
        accepted("task", "block", promote_b, "--reason", "Third promotion hold.")
        .get("data", {})
        .get("hold_id")
    )
    if not all((p_a_hold_1, p_a_hold_2, p_b_hold)):
        raise RuntimeError("promotion fixture failed to obtain its complete hold map")
    promote_base = "installed-task-bulk-promote-v1"
    promote_args = (
        "task",
        "promote",
        promote_a,
        promote_b,
        "--reason",
        "Release the exact original hold map.",
        "--idempotency-key",
        promote_base,
        "--hold-for",
        f"{promote_a}={p_a_hold_1}",
        "--hold-for",
        f"{promote_a}={p_a_hold_2}",
        "--hold-for",
        f"{promote_b}={p_b_hold}",
    )
    promoted = accepted(*promote_args)
    promote_rows = result_rows(promoted, (promote_a, promote_b), expected_ok=2)
    promote_keys = {row["contract_id"]: row["idempotency_key"] for row in promote_rows}
    if promote_keys != {
        promote_a: f"{promote_base}:promote:{promote_a}",
        promote_b: f"{promote_base}:promote:{promote_b}",
    }:
        raise RuntimeError("bulk promote changed its legacy per-target key derivation")
    verify_candidate_rows(promote_rows, promote_keys)
    p_a_later_hold = (
        accepted(
            "task", "block", promote_a, "--reason", "Later hold survives old replay."
        )
        .get("data", {})
        .get("hold_id")
    )
    replay_promote = accepted(*promote_args)
    promote_replay_rows = result_rows(
        replay_promote, (promote_a, promote_b), expected_ok=2
    )
    for row in promote_replay_rows:
        if not row.get("ok"):
            raise RuntimeError("exact bulk promotion replay rejected an old target")
        prior = next(
            item for item in promote_rows if item["contract_id"] == row["contract_id"]
        )
        if row.get("candidate_id") != prior.get("candidate_id") or row.get(
            "fact_ids"
        ) != prior.get("fact_ids"):
            raise RuntimeError("promotion replay changed its original durable facts")
    row_a = next(row for row in promote_replay_rows if row["contract_id"] == promote_a)
    promote_a_data = row_a.get("data", {})
    if p_a_later_hold not in promote_a_data.get("remaining_hold_ids", []):
        raise RuntimeError("bulk promotion replay released a later hold")
    verify_candidate_rows(promote_replay_rows, promote_keys)
    single_promote_replay = accepted(
        "task",
        "promote",
        promote_a,
        "--reason",
        "Release the exact original hold map.",
        "--idempotency-key",
        promote_keys[promote_a],
        "--hold-for",
        f"{promote_a}={p_a_hold_1}",
        "--hold-for",
        f"{promote_a}={p_a_hold_2}",
    )
    single_promote_data = single_promote_replay.get("data", {})
    if (
        single_promote_data.get("candidate_id") != row_a.get("candidate_id")
        or single_promote_data.get("fact_ids") != row_a.get("fact_ids")
        or single_promote_data.get("remaining_hold_ids") != [p_a_later_hold]
    ):
        raise RuntimeError("one-target promotion replay by returned key changed facts")

    # The new multi-target cancellation runs against signed local candidates,
    # including a task with a live Worker claim.
    cancel_a = create_task("bulk-cancel-a", "bulk-cancel-a-output")
    cancel_claimed = create_task("bulk-cancel-claimed", "bulk-cancel-claimed-output")
    cancel_package = accepted(
        "worker",
        "next",
        "--worker-id",
        "installed-bulk-cancel-worker",
        "--contract",
        cancel_claimed,
    )
    if cancel_package.get("contract_id") != cancel_claimed or not cancel_package.get(
        "claim_id"
    ):
        raise RuntimeError("cancellation fixture failed to create a live claim")
    cancel_targets = (cancel_a, cancel_claimed)
    cancel = accepted(
        "task",
        "cancel",
        *cancel_targets,
        "--reason",
        "Installed bulk cancellation fixture.",
        "--idempotency-key",
        "installed-task-bulk-cancel-v1",
    )
    cancel_rows = result_rows(cancel, cancel_targets, expected_ok=2)
    cancel_base = cancel.get("data", {}).get("batch_idempotency_key")
    if cancel_base != "installed-task-bulk-cancel-v1":
        raise RuntimeError("bulk cancel did not return its explicit base key")
    cancel_base = cancel.get("data", {}).get("batch_idempotency_key")
    if not isinstance(cancel_base, str) or not cancel_base:
        raise RuntimeError("bulk cancel omitted its generated batch key")
    cancel_keys = {row["contract_id"]: row["idempotency_key"] for row in cancel_rows}
    if set(cancel_keys) != set(cancel_targets) or len(set(cancel_keys.values())) != 2:
        raise RuntimeError("bulk cancel omitted distinct per-target keys")
    if cancel_keys != {
        contract_id: derived_key("cancel", cancel_base, contract_id)
        for contract_id in cancel_targets
    }:
        raise RuntimeError("bulk cancel did not return canonical per-target keys")
    verify_candidate_rows(cancel_rows, cancel_keys)
    before_cancel_replay = evidence()
    replay_cancel = accepted(
        "task",
        "cancel",
        *reversed(cancel_targets),
        "--reason",
        "Installed bulk cancellation fixture.",
        "--idempotency-key",
        cancel_base,
    )
    replay_cancel_rows = result_rows(
        replay_cancel, tuple(reversed(cancel_targets)), expected_ok=2
    )
    if evidence()["record_ids"] != before_cancel_replay["record_ids"]:
        raise RuntimeError("reordered cancel replay appended facts")
    for row in replay_cancel_rows:
        original = next(
            item for item in cancel_rows if item["contract_id"] == row["contract_id"]
        )
        if (
            row.get("idempotency_key") != original.get("idempotency_key")
            or row.get("candidate_id") != original.get("candidate_id")
            or row.get("fact_ids") != original.get("fact_ids")
            or row.get("data") != original.get("data")
        ):
            raise RuntimeError("reordered cancel replay changed durable outcome fields")
    single_cancel_replay = accepted(
        "task",
        "cancel",
        cancel_a,
        "--reason",
        "Installed bulk cancellation fixture.",
        "--idempotency-key",
        cancel_keys[cancel_a],
    )
    single_data = single_cancel_replay.get("data", {})
    original_single = next(row for row in cancel_rows if row["contract_id"] == cancel_a)
    if (
        single_data.get("record_id") != original_single.get("data", {}).get("record_id")
        or single_data.get("candidate_id") != original_single.get("candidate_id")
        or single_data.get("fact_ids") != original_single.get("fact_ids")
        or single_data.get("idempotency_key") != cancel_keys[cancel_a]
    ):
        raise RuntimeError("one-target replay by returned Candidate key changed facts")
    if evidence()["record_ids"] != before_cancel_replay["record_ids"]:
        raise RuntimeError("one-target cancel replay appended facts")
    changed_cancel = failure_json(
        "task",
        "cancel",
        *cancel_targets,
        "--reason",
        "Changed payload must not replay the original cancellation.",
        "--idempotency-key",
        cancel_base,
    )
    changed_rows = result_rows(changed_cancel, cancel_targets, expected_ok=0)
    for row in changed_rows:
        if row.get("candidate_id") == next(
            item["candidate_id"]
            for item in cancel_rows
            if item["contract_id"] == row["contract_id"]
        ):
            raise RuntimeError(
                "changed cancellation payload reused the old Candidate ID"
            )
        if row.get("outcome") == "accepted":
            raise RuntimeError("changed cancellation payload was reported as accepted")
    changed_state = evidence()
    for contract_id in cancel_targets:
        terminal = [
            record
            for record in changed_state["records"]
            if record["record_type"] == "lifecycle.terminal"
            and record["payload"].get("contract_id") == contract_id
        ]
        if len(terminal) != 1 or terminal[0]["payload"].get("terminal") != "cancelled":
            raise RuntimeError("changed cancellation payload altered terminal history")
    for contract_id in cancel_targets:
        status = accepted("task", "status", contract_id)
        if status.get("status") != "cancelled":
            raise RuntimeError("bulk cancellation did not terminalize every target")
    if success_json_value(
        "worker",
        "next",
        "--worker-id",
        "installed-bulk-cancel-worker",
        "--contract",
        cancel_claimed,
    ):
        raise RuntimeError("bulk-cancelled live claim became claimable again")

    # Single-target entry points keep their old direct-key/default behavior.
    legacy = create_task("bulk-legacy-single", "bulk-legacy-single-output")
    single_hold = (
        accepted("task", "block", legacy, "--reason", "Legacy single-target hold.")
        .get("data", {})
        .get("hold_id")
    )
    if not single_hold:
        raise RuntimeError("legacy single-target block omitted its hold ID")
    legacy_unblock = accepted(
        "task",
        "unblock",
        legacy,
        "--hold",
        single_hold,
        "--reason",
        "Legacy single-target release.",
    )
    if legacy_unblock.get("ok") is not True:
        raise RuntimeError("legacy single-target unblock result shape changed")
    legacy_cancel = accepted(
        "task", "cancel", legacy, "--reason", "Legacy single-target cancel."
    )
    if legacy_cancel.get("ok") is not True:
        raise RuntimeError("legacy single-target cancellation result shape changed")

    # Every durable task effect should appear in the required Redis event feed.
    event_fact_ids = {
        fact_id
        for rows in (block_rows, unblock_rows, promote_rows, cancel_rows)
        for row in rows
        if row.get("ok")
        for fact_id in row.get("fact_ids", [])
    }
    observed_event_ids: set[str] = set()
    for contract_id in (
        target_a,
        target_c,
        promote_a,
        promote_b,
        cancel_a,
        cancel_claimed,
    ):
        events = accepted(
            "task", "events", contract_id, "--after", "0", "--limit", "10000"
        )
        for event in events.get("data", []):
            if isinstance(event, dict) and isinstance(event.get("record_id"), str):
                observed_event_ids.add(event["record_id"])
    if not event_fact_ids.issubset(observed_event_ids):
        raise RuntimeError("required Redis task event feed omitted accepted bulk facts")

    rebuilt = json.loads(
        inspect_python("sqlite-reopen-rebuild", _REOPEN_REBUILD_SCRIPT, str(database))
    )
    if not all(
        rebuilt.get(key) is True
        for key in (
            "reopen_same_ids",
            "reopen_same_digest",
            "rebuild_same_ids",
            "rebuild_same_digest",
            "reopen_same_timestamps",
            "rebuild_same_timestamps",
        )
    ):
        raise RuntimeError("bulk control facts changed across reopen or rebuild")

    return {
        "status": "passed",
        "domain_initialized": True,
        "block_targets": list(block_targets),
        "block_success_count": 2,
        "block_failure_outcomes": [
            row["outcome"] for row in block_rows if not row.get("ok")
        ],
        "block_candidate_ids": {
            row["contract_id"]: row["candidate_id"]
            for row in block_rows
            if row.get("ok")
        },
        "block_fact_ids": {
            row["contract_id"]: row["fact_ids"] for row in block_rows if row.get("ok")
        },
        "block_actual_key_count": len(block_keys),
        "block_reordered_subset_replay_verified": True,
        "block_one_target_returned_key_replay_verified": True,
        "unblock_candidate_ids": {
            row["contract_id"]: row["candidate_id"] for row in unblock_rows
        },
        "unblock_reordered_subset_replay_verified": True,
        "unblock_one_target_returned_key_replay_verified": True,
        "promotion_candidate_ids": {
            row["contract_id"]: row["candidate_id"] for row in promote_rows
        },
        "promotion_later_hold_preserved": True,
        "promotion_one_target_returned_key_replay_verified": True,
        "cancel_candidate_ids": {
            row["contract_id"]: row["candidate_id"] for row in cancel_rows
        },
        "cancel_reordered_replay_verified": True,
        "cancel_one_target_returned_key_replay_verified": True,
        "changed_cancel_payload_rejected": True,
        "claimed_task_cancelled": True,
        "legacy_single_controls": True,
        "redis_event_fact_count": len(event_fact_ids),
        "redis_event_observed_count": len(event_fact_ids & observed_event_ids),
        "reopen_rebuild": {
            key: rebuilt[key]
            for key in (
                "reopen_same_ids",
                "reopen_same_digest",
                "rebuild_same_ids",
                "rebuild_same_digest",
                "reopen_same_timestamps",
                "rebuild_same_timestamps",
                "record_count",
                "digest",
            )
        },
        "command_count": len(command_summaries),
        "commands": command_summaries,
    }
