"""Installed local Worker profile setup and ordinary signed fixture work.

The model adapter is configured against a local request counter and never
invoked. The later output is an explicit harness fixture, not model evidence.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import stat
import subprocess
from threading import Thread

from installed_assignment_smoke import _REBUILD_SCRIPT
from installed_smoke import _write_private_json, json_command, run
from installed_worker_coordination_smoke import (
    _assert_task_output,
    _CANDIDATE_SCRIPT,
    _python_json,
    _task,
)


_STATE_SCRIPT = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
store = SQLiteStore.snapshot_from_path(sys.argv[1])
try:
    registration = store.get_worker_registration(sys.argv[2])
    assert registration is not None
    print(json.dumps({
        "record_ids": [r.id for _, r in store.scan_runtime_records()],
        "version": registration.version,
        "profile_id": registration.profile_id,
        "actor_id": registration.actor_id,
        "key_id": registration.key_id,
        "active_claims": registration.active_claims,
    }))
finally:
    store.close()
"""

_PROVENANCE_SCRIPT = r"""
import json, sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.runtime_record import validate_runtime_record
store = SQLiteStore.snapshot_from_path(sys.argv[1])
try:
    records = tuple(r for _, r in store.scan_runtime_records())
    index = {r.id: r for r in records}
    registrations = [r for r in records
                     if r.record_type == "worker.registered"
                     and r.payload.get("worker_id") == sys.argv[2]]
    promotions = [r for r in records
                  if r.record_type == "worker.profile.promoted"
                  and r.payload.get("worker_id") == sys.argv[2]]
    assert len(registrations) == 2 and len(promotions) == 1
    prior, current = registrations
    audit = promotions[0]
    for fact in (*registrations, audit):
        validate_runtime_record(fact)
    assert set(audit.payload) == {
        "worker_id", "previous_registration_id", "registration_id",
        "previous_version", "version", "profile_id", "actor_id",
        "key_id", "candidate_id",
    }
    assert prior.payload["version"] == "1" and prior.payload["profile_id"] == ""
    assert current.payload["version"] == "2"
    assert current.payload["profile_id"] == "openai-compatible-v1"
    assert audit.payload["previous_registration_id"] == prior.id
    assert audit.payload["registration_id"] == current.id
    assert audit.payload["previous_version"] == "1"
    assert audit.payload["version"] == "2"
    assert audit.payload["profile_id"] == current.payload["profile_id"]
    receipt = index[current.causal_parents[0]]
    validate_runtime_record(receipt)
    assert receipt.record_type == "candidate.received"
    assert receipt.payload["candidate_id"] == audit.payload["candidate_id"]
    assert current.payload["registered_by"] == receipt.payload["actor_id"]
    assert tuple(receipt.payload["effect_types"]) == (
        "worker.profile.promote", "worker.register",
    )
    assert audit.payload["actor_id"] == current.payload["actor_id"]
    assert audit.payload["key_id"] == current.payload["key_id"]
    assert receipt.payload["actor_id"] and receipt.payload["key_id"]
    assert current.causal_parents == (receipt.id,)
    assert audit.causal_parents == (receipt.id, prior.id, current.id)
    committed = [r for r in records if r.record_type == "candidate.committed"
                 and r.payload.get("candidate_id") == audit.payload["candidate_id"]]
    assert len(committed) == 1
    validate_runtime_record(committed[0])
    assert list(committed[0].payload["committed_record_ids"]) == [audit.id, current.id]
    assert committed[0].causal_parents == (receipt.id, audit.id, current.id)
    for field in ("worker_id", "actor_id", "key_id", "capabilities",
                  "pools", "capacity", "enabled"):
        assert prior.payload[field] == current.payload[field]
    print(json.dumps({
        "registration_facts": 2, "promotion_facts": 1,
        "prior_registration_id": prior.id,
        "registration_id": current.id, "promotion_id": audit.id,
        "exact_receipt_and_paired_fact_provenance": True,
    }))
finally:
    store.close()
"""


def installed_worker_profile_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    work = cwd / "installed-worker-profile"
    work.mkdir(mode=0o700)
    worker_id = "installed-local-profile-worker"
    domain = json_command(aig, ["domain", "init"], cwd=work, env=env)
    assert (
        json_command(
            aig, ["worker", "next", "--worker-id", worker_id], cwd=work, env=env
        )
        is None
    )
    database = work / ".aig/store.db"

    def state() -> dict:
        return _python_json(
            python, _STATE_SCRIPT, [str(database), worker_id], cwd=work, env=env
        )

    before = state()
    assert before["version"] == "1" and before["profile_id"] == ""
    request_count = 0

    class RequestCounter(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal request_count
            request_count += 1
            self.send_response(500)
            self.end_headers()

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RequestCounter)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    setup_env = dict(env)
    setup_env.update(
        AIGINEERING_API_KEY="explicit-installed-setup-fixture",
        AIGINEERING_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1",
        AIGINEERING_MODEL="deepseek-flash",
    )

    def model_setup() -> dict:
        result = subprocess.run(
            [
                str(aig),
                "run",
                "--once",
                "--worker-id",
                worker_id,
                "--worker",
                "llm",
                "--model",
                "deepseek-flash",
                "--max-retries",
                "0",
                "--thinking-mode",
                "disabled",
                "--json",
            ],
            cwd=work,
            env=setup_env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 1, result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "idle", payload
        assert payload["cycles"] == [], payload
        return payload

    setup_results = []
    try:
        setup_results.append(model_setup())
        promoted = state()
        assert promoted["version"] == "2"
        assert promoted["profile_id"] == "openai-compatible-v1"
        assert promoted["actor_id"] == before["actor_id"]
        assert promoted["key_id"] == before["key_id"]
        setup_results.append(model_setup())
        assert state() == promoted
        assert request_count == 0
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        _write_private_json(
            work,
            "worker-profile-adapter-setup",
            {
                "classification": "local_model_adapter_setup_fixture",
                "completed_setups": len(setup_results),
                "provider_requests": request_count,
                "runtime_results": setup_results,
            },
        )

    discovery = json_command(
        aig, ["worker", "registrations", worker_id], cwd=work, env=env
    )
    assert discovery["ok"] is True
    assert discovery["data"]["current"]["version"] == "2"
    assert len(discovery["data"]["history"]) == 2
    assert len(discovery["data"]["promotions"]) == 1
    assert state() == promoted
    provenance = _python_json(
        python, _PROVENANCE_SCRIPT, [str(database), worker_id], cwd=work, env=env
    )

    output_name = "profile-seam-result"
    contract_id = _task(
        aig, work, "profile-seam-fixture", output_name, cwd=work, env=env
    )
    package = json_command(
        aig,
        ["worker", "next", "--worker-id", worker_id, "--contract", contract_id],
        cwd=work,
        env=env,
    )
    assert package["worker_registration_version"] == "2"
    package_path = work / "package.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    key_path = work / ".aig/identity" / f"{promoted['key_id']}.ed25519"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    content = "Explicit signed fixture: local profile setup and claim version agree."
    candidate = _python_json(
        python,
        _CANDIDATE_SCRIPT,
        [
            domain["domain_id"],
            worker_id,
            promoted["key_id"],
            str(key_path),
            "result",
            "profile-seam-result-1",
            str(package_path),
            "/exec " + json.dumps({"outputs": {output_name: content}}),
        ],
        cwd=work,
        env=env,
    )
    submitted = json.loads(
        run(
            [str(aig), "worker", "submit", "--json", json.dumps(candidate)],
            cwd=work,
            env=env,
        )
    )
    assert submitted["status"] == "accepted", submitted
    _assert_task_output(
        aig,
        work,
        contract_id,
        output_name,
        content,
        work / "fixture-result.txt",
        cwd=work,
        env=env,
    )
    final_state = state()
    final_history = json_command(
        aig, ["worker", "registrations", worker_id], cwd=work, env=env
    )["data"]
    events = json_command(
        aig, ["task", "events", "--limit", "10000"], cwd=work, env=env
    )["data"]
    promotion_event = next(
        event for event in events if event["record_id"] == provenance["promotion_id"]
    )
    assert worker_id in promotion_event["worker_ids"]
    rebuilt = _python_json(python, _REBUILD_SCRIPT, [str(database)], cwd=work, env=env)
    assert rebuilt == {"same_ids": True, "same_digest": True}, rebuilt
    assert state() == final_state
    json_command(aig, ["redis", "rebuild"], cwd=work, env=env)
    assert (
        json_command(aig, ["task", "events", "--limit", "10000"], cwd=work, env=env)[
            "data"
        ]
        == events
    )
    assert (
        json_command(aig, ["worker", "registrations", worker_id], cwd=work, env=env)[
            "data"
        ]
        == final_history
    )
    return {
        "classification": "installed_model_adapter_setup_and_signed_fixture",
        "model_adapter_invocations": 0,
        "provider_requests": request_count,
        "same_worker_and_key": True,
        "pull_only_to_concrete_profile": True,
        "repeat_launch_no_facts": True,
        "registration_discovery_read_only": True,
        "ordinary_fixture_claim_version": "2",
        "ordinary_signed_fixture_output": True,
        "sqlite_and_redis_rebuild_equal": True,
        "promotion_event_typed_worker_reference": True,
        "provenance": provenance,
    }
