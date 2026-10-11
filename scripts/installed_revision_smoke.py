"""Installed CLI evidence for bounded revision after an exact changes decision."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess


def task_revision_smoke(aig: Path, python: Path, *, cwd: Path, env: dict[str, str]):
    """Use explicit fixture Workers; this does not establish provider quality."""
    from installed_smoke import json_command, run

    def command(args):
        return json_command(aig, args, cwd=cwd, env=env)

    rubric = command(
        ["asset", "add", "--name", "revision-rubric", "--content", "Check the source."]
    )
    evidence = command(
        ["asset", "add", "--name", "revision-input", "--content", "Frozen evidence."]
    )
    policy = {
        "mode": "independent",
        "policy_version": "installed-revision-v1",
        "rubric_asset_ids": [rubric["id"]],
        "evidence_asset_ids": [evidence["id"]],
        "verifier_capabilities": ["verify.human"],
        "required_attestations": 1,
    }
    input_policy = {
        "required_signer": command(["asset", "show", "revision-input"])["signed_by"]
    }
    name = "installed-revision-report"
    source = command(
        [
            "task",
            "create",
            "--name",
            name,
            "--output",
            name,
            "--input",
            "revision-input",
            "--budget",
            "6",
            "--acceptance-policy",
            json.dumps(policy),
            "--sensitive-input-policy",
            json.dumps(input_policy),
        ]
    )["contract_id"]

    def produce(task_id, worker_id):
        result = subprocess.run(
            [
                str(aig),
                "run",
                "--task",
                task_id,
                "--worker",
                "mock",
                "--worker-id",
                worker_id,
                "--json",
            ],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        output = json.loads(result.stdout)
        if result.returncode != 1 or not output.get("cycles"):
            raise RuntimeError(
                f"independent output did not await review: {result.stdout}"
            )
        # Name lookup may return an older same-name attempt. The task projection
        # supplies the exact output belonging to this source/successor lineage.
        return output["outputs"][name]

    def review(task_id, target, verdict, key):
        request = command(
            [
                "task",
                "request-review",
                task_id,
                "--output",
                name,
                "--asset",
                target,
                "--reason",
                "Check the exact fixture.",
                "--idempotency-key",
                key,
            ]
        )["data"]
        command(
            [
                "run",
                "--task",
                request["child_contract_id"],
                "--worker",
                "mock",
                "--worker-id",
                f"installed-revision-reviewer-{verdict}",
            ]
        )
        report = command(["asset", "show", request["report_output"]])["id"]
        decision = command(
            [
                "task",
                "review",
                "decide",
                request["record_id"],
                "--report",
                report,
                "--verdict",
                verdict,
                "--reason",
                "The operator checked the fixture.",
                "--idempotency-key",
                f"{key}-decision",
            ]
        )["data"]
        return request, decision

    old_target = produce(source, "installed-revision-original")
    old_request, changes = review(source, old_target, "changes", "revision-first")
    args = [
        "task",
        "request-changes",
        changes["record_id"],
        "--reason",
        "Produce a corrected bounded report using the exact original input.",
        "--idempotency-key",
        "installed-revision-publish",
    ]
    revision = command(args)["data"]
    successor = revision["successor_id"]
    if (
        revision["asset_id"] != old_target
        or revision["decision_id"] != changes["record_id"]
    ):
        raise RuntimeError("revision lost the exact target or decision link")
    if command(["task", "status", source])["status"] != "cancelled":
        raise RuntimeError("revision did not cancel its source attempt")
    if command(args)["data"] != revision:
        raise RuntimeError("nullable-budget revision replay changed its original fact")
    if command(["task", "revisions", source])["data"] != [revision]:
        raise RuntimeError("revision history did not return its durable exact link")
    injected = command(
        [
            "asset",
            "add",
            "--name",
            "revision-input",
            "--content",
            "Later same-name input must not enter revision context.",
        ]
    )["id"]
    audit = json.loads(
        run(
            [
                str(python),
                "-c",
                _AUDIT,
                str(cwd / ".aig/store.db"),
                source,
                successor,
                evidence["id"],
                old_target,
                changes["report_asset_id"],
                injected,
            ],
            cwd=cwd,
            env=env,
        )
    )
    new_target = produce(successor, "installed-revision-corrected")
    if new_target == old_target:
        raise RuntimeError("revision reused the rejected target identity")
    new_request, accepted = review(successor, new_target, "accept", "revision-second")
    if command(["task", "status", successor])["status"] != "completed":
        raise RuntimeError(
            "fresh revision qualification did not complete the successor"
        )
    if command(["task", "status", source])["status"] != "cancelled":
        raise RuntimeError("successor qualification rewrote the source terminal")
    exported = cwd / "installed-revised-report.md"
    run(
        [str(aig), "task", "result", successor, "--to", str(exported)],
        cwd=cwd,
        env=env,
    )
    if not exported.is_file():
        raise RuntimeError("qualified revision did not export a usable result")
    command(["redis", "rebuild"])
    if command(args)["data"] != revision:
        raise RuntimeError("post-terminal replay or Redis rebuild changed the revision")
    for task_id in (source, successor):
        events = command(["task", "events", task_id, "--limit", "1000"])["data"]
        if revision["record_id"] not in {item["record_id"] for item in events}:
            raise RuntimeError("revision link absent from source/successor event scope")
    return {
        "evidence_kind": "fixture",
        "provider_calls": 0,
        "changes_revision_fresh_review_accept_export": "passed",
        "source_terminal_preserved": "passed",
        "exact_input_snapshot": audit,
        "replay_reopen_redis_rebuild_scoped_events": "passed",
        "source_id": source,
        "successor_id": successor,
        "revision_record_id": revision["record_id"],
        "review_request_ids": [old_request["record_id"], new_request["record_id"]],
        "accepted_asset_id": accepted["asset_id"],
    }


_AUDIT = """
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.disclosure import compute_disclosure
s = SQLiteStore(sys.argv[1])
source, child = s.get_contract(sys.argv[2]), s.get_contract(sys.argv[3])
evidence, target, report, injected = sys.argv[4:]
assert child.id.startswith('task:v6:') and child.parent_id == source.id
assert child.inputs == source.inputs and child.outputs == source.outputs
assert child.acceptance_policy == source.acceptance_policy
assert child.sensitive_input_policy['required_signer'] == source.sensitive_input_policy['required_signer']
assert child.sensitive_input_policy['input_asset_bindings']['revision-input'] == (evidence,)
assert {a.id for a in compute_disclosure(child, s)} == {evidence, target, report}
assert injected not in {a.id for a in compute_disclosure(child, s)}
assert not any(r.payload.get('contract_id') == child.id
               for _,r in s.scan_runtime_records(record_type='output.qualified'))
s.close()
print(json.dumps({'frozen_input': True, 'later_same_name_excluded': True,
                  'original_policy_preserved': True, 'fresh_qualification_required': True}))
"""
