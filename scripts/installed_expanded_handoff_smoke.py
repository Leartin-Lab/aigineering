"""Verify expanded handoff through the installed CLI (explicit fixture only)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from installed_smoke import json_command, run


def expanded_handoff_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    source = json_command(
        aig,
        [
            "task",
            "create",
            "--name",
            "installed-expanded-handoff",
            "--description",
            "Write the installed expanded-handoff fixture report.",
            "--input",
            "source",
            "--activation",
            "source",
            "--output",
            "installed-expanded-report",
            "--budget",
            "3",
        ],
        cwd=cwd,
        env=env,
    )["contract_id"]
    planned = subprocess.run(
        [
            str(aig),
            "run",
            "--task",
            source,
            "--wait-timeout",
            "0.000001",
            "--interval",
            "0.05",
            "--worker",
            "mock",
            "--worker-id",
            "installed-expanded-planner",
            "--mock-output",
            '/plan {"reason":"Split the report into ordinary runtime work."}',
            "--json",
        ],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if planned.returncode != 1:
        raise RuntimeError(
            "expected a retained nonterminal planning run "
            f"(exit={planned.returncode}): stdout={planned.stdout!r}; "
            f"stderr={planned.stderr!r}"
        )
    before = json_command(aig, ["task", "status", source], cwd=cwd, env=env)
    if before["budget_remaining"] != 0 or before["terminal"]:
        raise RuntimeError(
            "planning fixture did not exhaust the source's own allowance"
        )
    args = [
        "task",
        "handoff",
        source,
        "--reason",
        "Complete the same original report.",
        "--idempotency-key",
        "installed-expanded-handoff",
    ]
    handed = json_command(aig, args, cwd=cwd, env=env)
    link = handed["data"]
    successor = link["successor_id"]
    # Detailed identities and amounts are checked from installed runtime facts,
    # independently of the CLI's display formatting.
    proof = json.loads(
        run(
            [
                str(python),
                "-c",
                """
import json,sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.disclosure import compute_disclosure
s=SQLiteStore.snapshot_from_path(sys.argv[1]); source=s.get_contract(sys.argv[2]); successor=s.get_contract(sys.argv[3])
records=tuple(r for _,r in s.scan_runtime_records()); by_id={r.id:r for r in records}
link=next(r for r in records if r.record_type=='task.handed_off' and r.payload.get('successor_id')==successor.id)
returns=[r for r in records if r.record_type=='allowance.returned' and r.payload.get('candidate_id')==link.payload['candidate_id']]
donors=[s.get_contract(r.payload['from_contract_id']) for r in returns]
assert len(returns)==3 and sum(r.payload['amount'] for r in returns)==3
assert successor.budget==3 and successor.parent_id==source.id and successor.id.startswith('task:v6:')
assert allowance_available(source,records)==0 and all(allowance_available(d,records)==0 for d in donors)
assert {r.payload['contract_id'] for r in records if r.record_type=='lifecycle.terminal' and r.payload['terminal']=='cancelled'} >= {source.id,*(d.id for d in donors)}
assert successor.outputs==source.outputs and successor.worker_capabilities==source.worker_capabilities
assert successor.labels==source.labels and successor.minting_authority==source.minting_authority
assert {a.id for a in compute_disclosure(successor,s)}=={a.id for a in compute_disclosure(source,s)}
print(json.dumps({'returns':len(returns),'returned_amount':3,'source_original_available':0,'successor_budget':3,'cancelled_donors':sorted(d.id for d in donors),'return_ids':sorted(r.id for r in returns),'source':source.id,'successor':successor.id,'schema_version':s.schema_version}))
s.close()
""",
                str(cwd / ".aig/store.db"),
                source,
                successor,
            ],
            cwd=cwd,
            env=env,
        )
    )
    replay = json_command(aig, args, cwd=cwd, env=env)
    if replay["data"] != link:
        raise RuntimeError("exact replay changed expanded handoff facts")
    finished = json_command(
        aig,
        ["run", "--task", successor, "--worker", "mock"],
        cwd=cwd,
        env=env,
    )
    if finished["status"] != "completed":
        raise RuntimeError("ordinary successor did not finish")
    json_command(aig, ["redis", "rebuild"], cwd=cwd, env=env)
    history = json_command(aig, ["task", "handoffs", source], cwd=cwd, env=env)
    if history["data"] != [link]:
        raise RuntimeError("reopen/rebuild changed expanded handoff history")
    json_command(
        aig,
        [
            "task",
            "result",
            successor,
            "--output",
            "installed-expanded-report",
            "--to",
            str(cwd / "installed-expanded-report.md"),
        ],
        cwd=cwd,
        env=env,
    )
    if (
        json_command(aig, ["task", "status", source], cwd=cwd, env=env)["status"]
        != "cancelled"
    ):
        raise RuntimeError("successor completion changed the predecessor")
    return {
        **proof,
        "classification": "installed_explicit_fixture",
        "provider_calls": 0,
        "exact_replay": True,
        "reopen_rebuild_history": True,
        "ordinary_successor_completed": True,
        "result_exported": True,
    }
