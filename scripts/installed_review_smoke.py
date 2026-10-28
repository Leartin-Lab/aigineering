"""Installed-process evidence for exact policy-bound task review."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess


def task_review_smoke(aig: Path, python: Path, *, cwd: Path, env: dict[str, str]):
    """Exercise review using explicit fixture Workers, never provider defaults."""
    from installed_smoke import json_command, run

    rubric = json_command(
        aig,
        [
            "asset",
            "add",
            "--name",
            "installed-review-rubric",
            "--content",
            "Check exact target content against the cited source.",
        ],
        cwd=cwd,
        env=env,
    )
    evidence = json_command(
        aig,
        [
            "asset",
            "add",
            "--name",
            "installed-review-evidence",
            "--content",
            "Fixture source: a signed bounded report is required.",
        ],
        cwd=cwd,
        env=env,
    )
    policy = {
        "mode": "independent",
        "policy_version": "installed-review-v1",
        "rubric_asset_ids": [rubric["id"]],
        "evidence_asset_ids": [evidence["id"]],
        "verifier_capabilities": ["verify.human"],
        "required_attestations": 1,
    }
    outcomes = []
    for verdict in ("accept", "changes"):
        name = f"installed-reviewed-{verdict}"
        created = json_command(
            aig,
            [
                "task",
                "create",
                "--name",
                name,
                "--output",
                name,
                "--budget",
                "5",
                "--acceptance-policy",
                json.dumps(policy),
            ],
            cwd=cwd,
            env=env,
        )
        source_id = created["contract_id"]
        # An independent source has accepted output but is unfinished until review.
        producer = subprocess.run(
            [
                str(aig),
                "run",
                "--task",
                source_id,
                "--worker",
                "mock",
                "--worker-id",
                f"installed-producer-{verdict}",
                "--json",
            ],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if producer.returncode != 1:
            raise RuntimeError(
                f"policy task incorrectly completed before review: {producer.stdout}"
            )
        result = json.loads(producer.stdout)
        if result.get("status") == "completed" or not result.get("cycles"):
            raise RuntimeError(f"policy output execution was not observable: {result}")
        target = json_command(aig, ["asset", "show", name], cwd=cwd, env=env)
        target_id = target["id"]
        args = [
            "task",
            "request-review",
            source_id,
            "--output",
            name,
            "--asset",
            target_id,
            "--reason",
            "Check exact fixture evidence.",
            "--idempotency-key",
            f"installed-review-request-{verdict}",
        ]
        request = json_command(aig, args, cwd=cwd, env=env)["data"]
        reviewer_id = request["child_contract_id"]
        review_run = json_command(
            aig,
            [
                "run",
                "--task",
                reviewer_id,
                "--worker",
                "mock",
                "--worker-id",
                f"installed-reviewer-{verdict}",
            ],
            cwd=cwd,
            env=env,
        )
        if not review_run["ok"]:
            raise RuntimeError("ordinary reviewer report task did not execute")
        report = json_command(
            aig,
            ["asset", "show", request["report_output"]],
            cwd=cwd,
            env=env,
        )
        decide_args = [
            "task",
            "review",
            "decide",
            request["record_id"],
            "--report",
            report["id"],
            "--verdict",
            verdict,
            "--reason",
            "Fixture content checked by the operator.",
            "--idempotency-key",
            f"installed-review-decision-{verdict}",
        ]
        decided = json_command(aig, decide_args, cwd=cwd, env=env)["data"]
        if (
            decided["report_asset_id"] != report["id"]
            or decided["asset_id"] != target_id
        ):
            raise RuntimeError("review decision lost exact target/report references")
        if json_command(aig, args, cwd=cwd, env=env)["data"] != request:
            raise RuntimeError("review request replay changed its original fact")
        if json_command(aig, decide_args, cwd=cwd, env=env)["data"] != decided:
            raise RuntimeError("review decision replay changed its original fact")
        history = json_command(aig, ["task", "reviews", source_id], cwd=cwd, env=env)
        if history["data"] != [request, decided]:
            raise RuntimeError("review history did not preserve request and decision")
        status = json_command(aig, ["task", "status", source_id], cwd=cwd, env=env)
        if (status["status"] == "completed") != (verdict == "accept"):
            raise RuntimeError(
                f"review verdict did not preserve acceptance semantics: {status}"
            )
        audit = run(
            [
                str(python),
                "-c",
                _AUDIT,
                str(cwd / ".aig/store.db"),
                source_id,
                reviewer_id,
                target_id,
                report["id"],
                verdict,
            ],
            cwd=cwd,
            env=env,
        )
        outcomes.append(json.loads(audit))
        json_command(aig, ["redis", "rebuild"], cwd=cwd, env=env)
        rebuilt = json_command(aig, ["task", "reviews", source_id], cwd=cwd, env=env)
        if rebuilt["data"] != history["data"]:
            raise RuntimeError("Redis rebuild changed durable review history")
        for scoped_id in (source_id, reviewer_id):
            events = json_command(
                aig,
                ["task", "events", scoped_id, "--limit", "1000"],
                cwd=cwd,
                env=env,
            )
            ids = {item["record_id"] for item in events["data"]}
            if not {request["record_id"], decided["record_id"]}.issubset(ids):
                raise RuntimeError(
                    "review facts absent from source/reviewer event scope"
                )
    return {
        "request_report_decision": "passed",
        "exact_context_and_lineage": "passed",
        "accept_qualifies_changes_does_not": "passed",
        "replay_reopen": "passed",
        "redis_rebuild_and_task_events": "passed",
        "outcomes": outcomes,
        "provider_calls": 0,
    }


_AUDIT = """
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.acceptance import acceptance_policy_id
s = SQLiteStore(sys.argv[1])
source, reviewer = s.get_contract(sys.argv[2]), s.get_contract(sys.argv[3])
target, report = s.get_asset(sys.argv[4]), s.get_asset(sys.argv[5])
verdict = sys.argv[6]
expected = {target.id, *source.acceptance_policy['rubric_asset_ids'],
            *source.acceptance_policy['evidence_asset_ids']}
assert {a.id for a in compute_disclosure(reviewer, s)} == expected
assert reviewer.inputs == () and reviewer.tool_scope == ()
assert target.signed_by != report.signed_by and report.created_by == reviewer.id
qual = [r for _, r in s.scan_runtime_records(record_type='output.qualified')
        if r.payload.get('contract_id') == source.id]
assert bool(qual) == (verdict == 'accept')
if qual:
    assert qual[0].payload['asset_id'] == target.id
    assert qual[0].payload['policy_id'] == acceptance_policy_id(source.acceptance_policy)
s.close()
print(json.dumps({'verdict': verdict, 'exact_disclosure': True,
                  'distinct_signed_producers': True, 'qualification': bool(qual)}))
"""
