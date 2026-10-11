"""Installed-wheel smoke for signed Worker claim and lease coordination.

All execution in this helper is explicit fixture work. It does not call a
provider and keeps harness private keys inside its temporary workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess

from installed_smoke import json_command, run


_IDENTITY_SCRIPT = r"""
import json, os, sys
from pathlib import Path
from aigineering.core.signing import Ed25519Signer
directory = Path(sys.argv[1])
directory.mkdir(mode=0o700, parents=True, exist_ok=False)
signer = Ed25519Signer()
private_path = directory / "harness.ed25519"
fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    stream.write(signer.private_key_hex)
metadata = {
    "actor_id": sys.argv[2],
    "key_id": sys.argv[3],
    "signature_kind": signer.kind,
    "public_key": signer.signer_id,
}
print(json.dumps(metadata, sort_keys=True))
"""

_CANDIDATE_SCRIPT = r"""
import json, sys
from pathlib import Path
from aigineering.agent.harness import HarnessCandidateAdapter, candidate_json
from aigineering.core.signing import Ed25519Signer
from aigineering.protocol.candidate import ActorKey
from aigineering.protocol.package import WorkerPackage

domain_id, actor_id, key_id, private_path, operation, request_id = sys.argv[1:7]
signer = Ed25519Signer.from_private_key_hex(
    Path(private_path).read_text(encoding="utf-8").strip()
)
key = ActorKey(actor_id, key_id, signer.kind, signer.signer_id, ("worker.submit",))
adapter = HarnessCandidateAdapter(domain_id, key, signer)
if operation == "claim":
    proposal = adapter.claim_candidate(request_id=request_id, contract_id=None, lease_seconds=120)
elif operation == "renew":
    package = WorkerPackage.from_json(Path(sys.argv[7]).read_text(encoding="utf-8"))
    proposal = adapter.renewal_candidate(package, request_id=request_id, lease_seconds=120)
elif operation == "result":
    package = WorkerPackage.from_json(Path(sys.argv[7]).read_text(encoding="utf-8"))
    action = sys.argv[8]
    proposal = adapter.result_candidate(package, action, idempotency_key=request_id)
else:
    raise SystemExit("unknown operation")
print(candidate_json(proposal))
"""

_RENEW_SCRIPT = r"""
import json, sqlite3, sys
from pathlib import Path
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.candidate import candidate_proposal_from_dict

database, package_path, candidate_path, worker_id = sys.argv[1:]
package = WorkerPackage.from_json(Path(package_path).read_text(encoding="utf-8"))
candidate = candidate_proposal_from_dict(json.loads(Path(candidate_path).read_text(encoding="utf-8")))
store = SQLiteStore(database)
try:
    before = store.get_claim(package.contract_id)
    renewed = store.renew_claim(
        package.claim_id, package.claim_epoch, worker_id,
        lease_seconds=120, claim_candidate=candidate, runtime_records=(),
    )
    after_first = store.get_claim(package.contract_id)
    replay = store.renew_claim(
        package.claim_id, package.claim_epoch, worker_id,
        lease_seconds=120, claim_candidate=candidate, runtime_records=(),
    )
    after_replay = store.get_claim(package.contract_id)
    print(json.dumps({
        "renewed": renewed is not None,
        "replay_rejected_or_noop": replay is None,
        "lease_after_first": after_first.get("lease_until") if after_first else None,
        "lease_after_replay": after_replay.get("lease_until") if after_replay else None,
        "lease_before": before.get("lease_until") if before else None,
    }, sort_keys=True))
finally:
    store.close()
"""

_STATE_SCRIPT = r"""
import json, sqlite3, sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.immutability import deep_thaw

database = sys.argv[1]
store = SQLiteStore(database)
try:
    rows = store.scan_runtime_records()
    selected = []
    for revision, record in rows:
        if record.record_type in {
            "worker.registered", "worker.claim.requested",
            "worker.claim.renew.requested", "claim.granted", "claim.renewed",
            "claim.submitted", "candidate.committed",
        }:
            selected.append({
                "revision": revision,
                "record_id": record.id,
                "record_type": record.record_type,
                "payload": deep_thaw(record.payload),
            })
    ids_before = [record.id for _, record in rows]
    schema = store._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    store.rebuild_runtime_materializations()
    rows_after = store.scan_runtime_records()
    print(json.dumps({
        "schema_version": schema,
        "record_ids_before": ids_before,
        "record_ids_after": [record.id for _, record in rows_after],
        "claims": {
            contract.id: store.get_claim(contract.id)
            for contract in store.get_all_contracts()
        },
        "selected": selected,
    }, sort_keys=True))
finally:
    store.close()
"""

_LOCAL_RESULT_SCRIPT = r"""
import json, sys
from pathlib import Path
from aigineering.agent.harness import candidate_json
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import load_existing_local_worker_host
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.wire import asset_from_dict, contract_from_dict

database, package_path, worker_id, request_id, output_content = sys.argv[1:]
class LocalWorker:
    def __init__(self, worker_id): self.worker_id = worker_id
package = WorkerPackage.from_json(Path(package_path).read_text(encoding="utf-8"))
store = SQLiteStore(database)
try:
    host = load_existing_local_worker_host(store, LocalWorker(worker_id))
    contract = contract_from_dict(package.contract)
    assets = tuple(
        asset_from_dict(item)
        for item in (*package.disclosed_assets, *package.method_context_assets)
    )
    envelope = CandidateEnvelope(
        contract_id=package.contract_id,
        worker_id=worker_id,
        raw_output="/exec " + json.dumps({"outputs": {"local-pull-result": output_content}}),
        package_id=package.package_id,
        claim_id=package.claim_id,
        claim_epoch=package.claim_epoch,
        idempotency_key=request_id,
    )
    proposal = host.sign_envelope(
        envelope, contract=contract, disclosed_assets=assets,
        allowance=package.budget_remaining,
    )
    print(candidate_json(proposal))
finally:
    store.close()
"""


def _python_json(
    python: Path,
    script: str,
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict:
    output = run([str(python.resolve()), "-c", script, *args], cwd=cwd, env=env)
    result = json.loads(output)
    if not isinstance(result, dict):
        raise RuntimeError("installed Worker helper returned non-object JSON")
    return result


def _task(
    aig: Path,
    workspace: Path,
    name: str,
    output: str,
    *,
    cwd: Path,
    env: dict[str, str],
) -> str:
    result = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "create",
            "--name",
            name,
            "--description",
            f"Installed explicit Worker coordination fixture: {name}.",
            "--budget",
            "3",
            "--output",
            output,
        ],
        cwd=cwd,
        env=env,
    )
    return result["contract_id"]


def _assert_task_output(
    aig: Path,
    workspace: Path,
    contract_id: str,
    output_name: str,
    expected: str,
    destination: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> None:
    status = json_command(
        aig,
        ["-C", str(workspace), "task", "status", contract_id],
        cwd=cwd,
        env=env,
    )
    if status.get("status") not in {"complete", "completed"}:
        raise RuntimeError(f"fixture task did not reach completion: {status}")
    exported = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "result",
            contract_id,
            "--output",
            output_name,
            "--to",
            str(destination),
        ],
        cwd=cwd,
        env=env,
    )
    if not destination.is_file() or destination.read_text(encoding="utf-8") != expected:
        raise RuntimeError(f"fixture output was not exact after submission: {exported}")


def _fixture_files(directory: Path) -> Path:
    directory.mkdir()
    (directory / "coordination_worker.py").write_text(
        "import json\n"
        "from aigineering.protocol.types import Candidate\n"
        "class Worker:\n"
        "    def __init__(self, worker_id): self.worker_id = worker_id\n"
        "    def invoke(self, contract, disclosed_assets):\n"
        "        return Candidate(worker_id=self.worker_id, raw_output='/exec ' + "
        "json.dumps({'outputs': {name: 'fixture-output:' + name "
        "for name in contract.outputs}}))\n"
        "def build_worker(*, worker_id): return Worker(worker_id)\n",
        encoding="utf-8",
    )
    config = directory / "workers.toml"
    config.write_text(
        '[fleet]\ndb_path = ".aig/store.db"\npoll_interval = 0.01\n'
        '[[workers]]\nid = "coordination-fixture-worker"\nkind = "local"\n'
        'worker_factory = "coordination_worker:build_worker"\n',
        encoding="utf-8",
    )
    return config


def worker_coordination_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Exercise installed signed claims, renewals, submission, and rebuild."""
    workspace = cwd / "installed-worker-coordination"
    workspace.mkdir()
    database = workspace / ".aig" / "store.db"
    local_env = dict(env)
    local_env["AIG_ACTOR_KEY_FILE"] = str(
        workspace / ".aig" / "identity" / "root.ed25519"
    )
    fixture_dir = cwd / "worker-coordination-fixtures"
    workers = _fixture_files(fixture_dir)
    local_env["PYTHONPATH"] = str(fixture_dir)

    initialized = json_command(
        aig, ["-C", str(workspace), "domain", "init"], cwd=cwd, env=local_env
    )
    domain_id = initialized.get("domain_id")
    if not isinstance(domain_id, str) or not domain_id:
        domain_info = json_command(
            aig, ["-C", str(workspace), "domain", "show"], cwd=cwd, env=local_env
        )
        domain_id = domain_info.get("domain_id")
    if not isinstance(domain_id, str) or not domain_id:
        raise RuntimeError("installed domain init did not expose its domain ID")

    # The no-task scan is deliberately made before any task exists. It must not
    # write a receipt or consume this same signed null-target claim Candidate.
    actor_id = "installed-harness-worker"
    key_id = "installed-harness-key"
    private_dir = workspace / ".private-harness"
    metadata = _python_json(
        python,
        _IDENTITY_SCRIPT,
        [str(private_dir), actor_id, key_id],
        cwd=cwd,
        env=local_env,
    )
    private_key_path = private_dir / "harness.ed25519"
    if stat.S_IMODE(private_key_path.stat().st_mode) != 0o600:
        raise RuntimeError("harness private key was not created with mode 0600")
    registration = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "worker",
            "register",
            "--worker-id",
            actor_id,
            "--capacity",
            "2",
            "--key-id",
            key_id,
            "--public-key",
            metadata["public_key"],
            "--signature-kind",
            metadata["signature_kind"],
        ],
        cwd=cwd,
        env=local_env,
    )
    if registration.get("actor_id") != actor_id or registration.get("key_id") != key_id:
        raise RuntimeError("registered harness identity did not match its public key")

    claim_candidate = run(
        [
            str(python.resolve()),
            "-c",
            _CANDIDATE_SCRIPT,
            domain_id,
            actor_id,
            key_id,
            str(private_key_path),
            "claim",
            "coordination:claim:initial",
        ],
        cwd=cwd,
        env=local_env,
    )
    empty_before = _python_json(
        python, _STATE_SCRIPT, [str(database)], cwd=cwd, env=local_env
    )
    empty_result = json_command(
        aig,
        ["-C", str(workspace), "worker", "next", "--candidate", claim_candidate],
        cwd=cwd,
        env=local_env,
    )
    empty_after = _python_json(
        python, _STATE_SCRIPT, [str(database)], cwd=cwd, env=local_env
    )
    if empty_before["record_ids_before"] != empty_after["record_ids_before"]:
        raise RuntimeError("no-work signed scan persisted worker coordination facts")
    if empty_result not in (None, {}) and empty_result.get("status") not in {
        "no_work",
        "idle",
        "empty",
    }:
        raise RuntimeError(
            f"empty-domain scan unexpectedly returned work: {empty_result}"
        )

    first_task = _task(
        aig, workspace, "signed-claim-first", "first-result", cwd=cwd, env=local_env
    )
    first_package = json_command(
        aig,
        ["-C", str(workspace), "worker", "next", "--candidate", claim_candidate],
        cwd=cwd,
        env=local_env,
    )
    if first_package.get("contract_id") != first_task or not first_package.get(
        "claim_id"
    ):
        raise RuntimeError(
            f"same no-work Candidate did not claim new work: {first_package}"
        )

    second_task = _task(
        aig,
        workspace,
        "claim-idempotency-probe",
        "second-result",
        cwd=cwd,
        env=local_env,
    )
    before_replay = _python_json(
        python, _STATE_SCRIPT, [str(database)], cwd=cwd, env=local_env
    )
    if before_replay["claims"].get(second_task) is not None:
        raise RuntimeError(
            "second eligible task was already claimed before replay probe"
        )
    exact_replay = json_command(
        aig,
        ["-C", str(workspace), "worker", "next", "--candidate", claim_candidate],
        cwd=cwd,
        env=local_env,
    )
    state_after_replay = _python_json(
        python, _STATE_SCRIPT, [str(database)], cwd=cwd, env=local_env
    )
    if state_after_replay["record_ids_before"] != before_replay["record_ids_before"]:
        raise RuntimeError("exact Candidate replay added facts or another receipt")
    if state_after_replay["claims"].get(second_task) is not None:
        raise RuntimeError("exact Candidate replay claimed the second eligible task")
    claims_after_replay = [
        item
        for item in state_after_replay["selected"]
        if item["record_type"] == "worker.claim.requested"
        and item["payload"].get("candidate_id") == json.loads(claim_candidate)["id"]
    ]
    if len(claims_after_replay) != 1:
        raise RuntimeError("exact claim replay did not preserve a single claim receipt")
    if exact_replay is not None and exact_replay.get("contract_id") not in {
        first_task,
        None,
    }:
        raise RuntimeError(
            "replaying a claim Candidate acquired a second eligible task"
        )

    # Supplying an explicit identity that disagrees with the signed Worker is
    # rejected before any claim facts can be written.
    before_mismatch = state_after_replay["record_ids_before"]
    malformed = subprocess.run(
        [
            str(aig),
            "-C",
            str(workspace),
            "worker",
            "next",
            "--candidate",
            "{malformed",
            "--json",
        ],
        cwd=cwd,
        env=local_env,
        check=False,
        text=True,
        capture_output=True,
    )
    if malformed.returncode == 0:
        raise RuntimeError("worker next accepted malformed signed Candidate JSON")
    after_malformed = _python_json(
        python, _STATE_SCRIPT, [str(database)], cwd=cwd, env=local_env
    )["record_ids_before"]
    if before_mismatch != after_malformed:
        raise RuntimeError("malformed Candidate wrote claim or receipt facts")
    mismatch = subprocess.run(
        [
            str(aig),
            "-C",
            str(workspace),
            "worker",
            "next",
            "--candidate",
            claim_candidate,
            "--worker-id",
            "other-worker",
            "--json",
        ],
        cwd=cwd,
        env=local_env,
        check=False,
        text=True,
        capture_output=True,
    )
    if mismatch.returncode == 0:
        raise RuntimeError(
            "worker next accepted CLI identity flags conflicting with signature"
        )
    after_mismatch = _python_json(
        python, _STATE_SCRIPT, [str(database)], cwd=cwd, env=local_env
    )["record_ids_before"]
    if before_mismatch != after_mismatch:
        raise RuntimeError("mismatched worker flags wrote claim or receipt facts")

    # Renewal is signed by the registered harness key and is single-use. The
    # same Candidate replay must not extend the lease a second time.
    package_path = workspace / "claimed-package.json"
    package_path.write_text(json.dumps(first_package), encoding="utf-8")
    renewal_candidate = run(
        [
            str(python.resolve()),
            "-c",
            _CANDIDATE_SCRIPT,
            domain_id,
            actor_id,
            key_id,
            str(private_key_path),
            "renew",
            "coordination:renew:first",
            str(package_path),
        ],
        cwd=cwd,
        env=local_env,
    )
    renewal_candidate_path = workspace / "renewal-candidate.json"
    renewal_candidate_path.write_text(renewal_candidate, encoding="utf-8")
    renewal = _python_json(
        python,
        _RENEW_SCRIPT,
        [str(database), str(package_path), str(renewal_candidate_path), actor_id],
        cwd=cwd,
        env=local_env,
    )
    if (
        renewal.get("renewed") is not True
        or renewal.get("replay_rejected_or_noop") is not True
        or renewal.get("lease_after_first") != renewal.get("lease_after_replay")
    ):
        raise RuntimeError(
            f"signed claim renewal replay changed durable lease: {renewal}"
        )

    result_candidate = run(
        [
            str(python.resolve()),
            "-c",
            _CANDIDATE_SCRIPT,
            domain_id,
            actor_id,
            key_id,
            str(private_key_path),
            "result",
            "coordination:result:first",
            str(package_path),
            '/exec {"outputs": {"first-result": "harness-submitted-output"}}',
        ],
        cwd=cwd,
        env=local_env,
    )
    submitted = json.loads(
        run(
            [
                str(aig),
                "-C",
                str(workspace),
                "worker",
                "submit",
                "--json",
                result_candidate,
            ],
            cwd=cwd,
            env=local_env,
        )
    )
    if submitted.get("status") != "accepted" or "first-result" not in submitted.get(
        "accepted", []
    ):
        raise RuntimeError(f"ordinary Worker Candidate submission failed: {submitted}")
    _assert_task_output(
        aig,
        workspace,
        first_task,
        "first-result",
        "harness-submitted-output",
        workspace / "first-result.export.txt",
        cwd=cwd,
        env=local_env,
    )

    # The second signed claim Candidate is distinct. This ensures the replay
    # test above had newly eligible work available instead of relying on full
    # worker capacity or an empty queue.
    second_claim_candidate = run(
        [
            str(python.resolve()),
            "-c",
            _CANDIDATE_SCRIPT,
            domain_id,
            actor_id,
            key_id,
            str(private_key_path),
            "claim",
            "coordination:claim:second",
        ],
        cwd=cwd,
        env=local_env,
    )
    second_package = json_command(
        aig,
        ["-C", str(workspace), "worker", "next", "--candidate", second_claim_candidate],
        cwd=cwd,
        env=local_env,
    )
    if second_package.get("contract_id") != second_task:
        raise RuntimeError(
            "a fresh single-use claim did not acquire the remaining task"
        )
    second_package_path = workspace / "second-package.json"
    second_package_path.write_text(json.dumps(second_package), encoding="utf-8")
    second_result_candidate = run(
        [
            str(python.resolve()),
            "-c",
            _CANDIDATE_SCRIPT,
            domain_id,
            actor_id,
            key_id,
            str(private_key_path),
            "result",
            "coordination:result:second",
            str(second_package_path),
            '/exec {"outputs": {"second-result": "harness-submitted-output"}}',
        ],
        cwd=cwd,
        env=local_env,
    )
    second_submission = json.loads(
        run(
            [
                str(aig),
                "-C",
                str(workspace),
                "worker",
                "submit",
                "--json",
                second_result_candidate,
            ],
            cwd=cwd,
            env=local_env,
        )
    )
    if second_submission.get(
        "status"
    ) != "accepted" or "second-result" not in second_submission.get("accepted", []):
        raise RuntimeError(
            f"second ordinary Worker Candidate submission failed: {second_submission}"
        )
    _assert_task_output(
        aig,
        workspace,
        second_task,
        "second-result",
        "harness-submitted-output",
        workspace / "second-result.export.txt",
        cwd=cwd,
        env=local_env,
    )

    # Exercise both normal fixture entry points; these tasks are independent
    # explicit fixtures, not provider calls or production mock defaults.
    run_task = _task(
        aig, workspace, "normal-run-fixture", "run-result", cwd=cwd, env=local_env
    )
    run_result = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "run",
            "--config",
            str(workers),
            "--resume",
            run_task,
        ],
        cwd=cwd,
        env=local_env,
    )
    if run_result.get("completed") is not True:
        raise RuntimeError(
            f"installed normal run fixture did not complete: {run_result}"
        )
    _assert_task_output(
        aig,
        workspace,
        run_task,
        "run-result",
        "fixture-output:run-result",
        workspace / "run-result.export.txt",
        cwd=cwd,
        env=local_env,
    )
    fleet_task = _task(
        aig, workspace, "normal-fleet-fixture", "fleet-result", cwd=cwd, env=local_env
    )
    fleet_result = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "fleet",
            "run",
            "--config",
            str(workers),
            "--task",
            fleet_task,
        ],
        cwd=cwd,
        env=local_env,
    )
    if fleet_result.get("completed") is not True:
        raise RuntimeError(f"installed fleet fixture did not complete: {fleet_result}")
    _assert_task_output(
        aig,
        workspace,
        fleet_task,
        "fleet-result",
        "fixture-output:fleet-result",
        workspace / "fleet-result.export.txt",
        cwd=cwd,
        env=local_env,
    )

    # Default local-pull path uses the workspace's owned root identity file.
    # Its local Worker key is generated by the runtime and proved by the
    # resulting claim receipt rather than by guessing a key filename.
    local_pull_task = _task(
        aig,
        workspace,
        "default-local-pull",
        "local-pull-result",
        cwd=cwd,
        env=local_env,
    )
    local_package = json_command(
        aig, ["-C", str(workspace), "worker", "next"], cwd=cwd, env=local_env
    )
    if local_package.get("contract_id") != local_pull_task or not local_package.get(
        "claim_id"
    ):
        raise RuntimeError(
            f"default local pull did not claim its fixture task: {local_package}"
        )
    local_package_path = workspace / "local-pull-package.json"
    local_package_path.write_text(json.dumps(local_package), encoding="utf-8")
    local_result_candidate = run(
        [
            str(python.resolve()),
            "-c",
            _LOCAL_RESULT_SCRIPT,
            str(database),
            str(local_package_path),
            "cli-worker",
            "coordination:local-pull:result",
            "explicit-local-fixture",
        ],
        cwd=cwd,
        env=local_env,
    )
    local_submission = json.loads(
        run(
            [
                str(aig),
                "-C",
                str(workspace),
                "worker",
                "submit",
                "--json",
                local_result_candidate,
            ],
            cwd=cwd,
            env=local_env,
        )
    )
    if local_submission.get(
        "status"
    ) != "accepted" or "local-pull-result" not in local_submission.get("accepted", []):
        raise RuntimeError(
            f"default-pull task did not accept its signed fixture result: {local_submission}"
        )
    _assert_task_output(
        aig,
        workspace,
        local_pull_task,
        "local-pull-result",
        "explicit-local-fixture",
        workspace / "local-pull-result.export.txt",
        cwd=cwd,
        env=local_env,
    )

    before_rebuild = _python_json(
        python, _STATE_SCRIPT, [str(database)], cwd=cwd, env=local_env
    )
    if before_rebuild.get("schema_version") != 21:
        raise RuntimeError(
            f"Installed Worker coordination requires schema 21: {before_rebuild.get('schema_version')}"
        )
    if before_rebuild["record_ids_before"] != before_rebuild["record_ids_after"]:
        raise RuntimeError("reopen/rebuild changed Worker coordination fact IDs")
    claim_facts = [
        item
        for item in before_rebuild["selected"]
        if item["record_type"] == "worker.claim.requested"
    ]
    if not any(
        item["payload"].get("worker_id") == actor_id
        and item["payload"].get("key_id") == key_id
        for item in claim_facts
    ):
        raise RuntimeError(
            "signed claim receipt omitted the registered Worker identity"
        )
    registrations = [
        item
        for item in before_rebuild["selected"]
        if item["record_type"] == "worker.registered"
    ]
    fixture_claims = [
        item
        for item in claim_facts
        if item["payload"].get("worker_id") == "coordination-fixture-worker"
    ]
    if len(fixture_claims) < 2 or not any(
        registration_item["payload"].get("worker_id") == "coordination-fixture-worker"
        and registration_item["payload"].get("key_id")
        == fixture_claims[0]["payload"].get("key_id")
        for registration_item in registrations
    ):
        raise RuntimeError(
            "normal run/fleet claims lack matching registered Worker receipts"
        )
    if not any(
        item["payload"].get("worker_id") == "cli-worker" for item in claim_facts
    ):
        raise RuntimeError(
            "default local pull lacks its signed cli-worker claim receipt"
        )

    first_grants = [
        item
        for item in before_rebuild["selected"]
        if item["record_type"] == "claim.granted"
        and item["payload"].get("claim_id") == first_package["claim_id"]
    ]
    if (
        len(first_grants) != 1
        or first_grants[0]["payload"].get("contract_id") != first_task
    ):
        raise RuntimeError("claim grant fact did not bind the exact first task")

    # The null-target command receipt belongs to the global feed. The durable
    # claim grant is separately scoped to the exact task tree.
    first_events = json_command(
        aig,
        ["-C", str(workspace), "task", "events", first_task, "--limit", "10000"],
        cwd=cwd,
        env=local_env,
    )
    first_task_event_ids = [
        item.get("record_id") for item in first_events.get("data", [])
    ]
    if first_grants[0]["record_id"] not in first_task_event_ids:
        raise RuntimeError("task event feed omitted the exact claim grant")
    global_events = json_command(
        aig,
        ["-C", str(workspace), "task", "events", "--limit", "10000"],
        cwd=cwd,
        env=local_env,
    )
    global_event_ids = [item.get("record_id") for item in global_events.get("data", [])]
    if claims_after_replay[0]["record_id"] not in global_event_ids:
        raise RuntimeError(
            "global event feed omitted the null-target claim request receipt"
        )
    json_command(
        aig, ["-C", str(workspace), "redis", "rebuild"], cwd=cwd, env=local_env
    )
    after_redis_rebuild = json_command(
        aig,
        ["-C", str(workspace), "task", "events", first_task, "--limit", "10000"],
        cwd=cwd,
        env=local_env,
    )
    after_task_ids = [
        item.get("record_id") for item in after_redis_rebuild.get("data", [])
    ]
    after_global_events = json_command(
        aig,
        ["-C", str(workspace), "task", "events", "--limit", "10000"],
        cwd=cwd,
        env=local_env,
    )
    after_global_ids = [
        item.get("record_id") for item in after_global_events.get("data", [])
    ]
    if after_task_ids != first_task_event_ids or after_global_ids != global_event_ids:
        raise RuntimeError("Redis rebuild changed scoped or global Worker event IDs")
    redis_status = json_command(
        aig, ["-C", str(workspace), "redis", "status"], cwd=cwd, env=local_env
    )
    if (
        not redis_status.get("ok")
        or not redis_status.get("available")
        or not redis_status.get("current")
    ):
        raise RuntimeError(
            f"installed Redis relay is not healthy after rebuild: {redis_status}"
        )

    return {
        "classification": "installed_explicit_fixture",
        "workspace": str(workspace),
        "domain_id": domain_id,
        "harness_worker_id": actor_id,
        "harness_key_id": key_id,
        "first_contract_id": first_task,
        "second_contract_id": second_task,
        "claim_receipt_id": claims_after_replay[0]["record_id"],
        "signed_empty_scan_did_not_write": True,
        "same_candidate_retried_after_task_creation": True,
        "exact_replay_did_not_claim_second_eligible_task": True,
        "mismatched_flags_rejected_without_facts": True,
        "signed_renewal_replay_did_not_extend": True,
        "ordinary_signed_result_submission": True,
        "default_local_pull_completed_by_explicit_fixture": True,
        "normal_run_and_fleet_fixtures_completed": True,
        "rebuild_fact_ids_unchanged": True,
        "redis_event_index_current_after_rebuild": True,
        "schema_version": 21,
        "provider_calls": 0,
    }
