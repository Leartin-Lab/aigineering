"""Frozen fresh reassignment requests recover the original signed intent."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil

from click import Group
from click.testing import CliRunner
import pytest

from aigineering.cli.task_assignment import register_task_assignment_commands
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.local_identity import ensure_local_domain, load_actor_signer
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.identity import canonical_json


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_assignment_commands(task)
    root.add_command(task)
    return root


def _workspace(tmp_path: Path, monkeypatch, count: int = 3):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    root_key = database.parent / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key))
    store = SQLiteStore(database)
    genesis = ensure_local_domain(store)
    root_signer = load_actor_signer(root_key)
    contracts = []
    for index in range(count):
        contract = build_control_plane_contract(
            name=f"request target {index}",
            description="A reassignment-request test target.",
            outputs=("result",),
            budget=3,
        )
        result = publish_effect(
            store,
            store,
            genesis,
            genesis.root_keys[0],
            root_signer,
            contract_declaration_effect(contract),
            idempotency_key=f"request-contract-{index}",
        )
        assert result.accepted
        contracts.append(contract)
    store.register_worker(WorkerRegistration("worker:one", capacity=4))
    store.register_worker(WorkerRegistration("worker:two", capacity=4))
    store.close()

    import aigineering.application
    import aigineering.cli.task_assignment as cli_assignment
    import aigineering.task_assignment as app_assignment

    class _Collaboration:
        def sync(self):
            return None

    for module in (aigineering.application, cli_assignment, app_assignment):
        monkeypatch.setattr(module, "require_redis", lambda: None)
        monkeypatch.setattr(
            module, "collaboration_service", lambda _store: _Collaboration()
        )
    monkeypatch.setattr(app_assignment, "sync_runtime", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        cli_assignment,
        "_persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        cli_assignment, "_persistent_store", lambda: SQLiteStore(database)
    )
    monkeypatch.chdir(tmp_path)
    return database, tuple(contracts)


def _json_result(runner: CliRunner, args: list[str], *, expected: int = 0) -> dict:
    result = runner.invoke(_app(), args)
    assert result.exit_code == expected, result.output
    return json.loads(result.output)


def _seed_assignment(runner: CliRunner, contract_id: str, suffix: str) -> str:
    result = _json_result(
        runner,
        [
            "task",
            "assign",
            contract_id,
            "worker:one",
            "--reason",
            "seed owner",
            "--idempotency-key",
            f"request-seed-{suffix}",
            "--json",
        ],
    )
    return result["data"]["record_id"]


def _assignment_facts(database: Path):
    store = SQLiteStore.snapshot_from_path(database)
    try:
        return tuple(
            record
            for _revision, record in store.scan_runtime_records(
                record_type="claim.assignment.changed"
            )
        )
    finally:
        store.close()


def test_current_request_freezes_and_resumes_exact_intent_after_later_head(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace with ; and $()"
    database, contracts = _workspace(workspace, monkeypatch, count=2)
    runner = CliRunner()
    first_head = _seed_assignment(runner, contracts[0].id, "one")
    second_head = _seed_assignment(runner, contracts[1].id, "two")

    result = runner.invoke(
        _app(),
        [
            "task",
            "reassign",
            contracts[0].id,
            contracts[1].id,
            "--current",
            "--worker",
            "worker:two",
            "--reason",
            "rotate after freeze",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    response = json.loads(result.stdout)
    assert "Resume with: aig -C " in result.stderr
    assert "rotate after freeze" not in result.stderr
    request_id = response["data"]["request_id"]
    rows = response["data"]["results"]
    assert all(row["request_id"] == request_id for row in rows)
    assert [row["requested_previous_assignment_id"] for row in rows] == [
        first_head,
        second_head,
    ]

    request_path = (
        workspace / ".aig" / "requests" / "reassign" / f"request-{request_id}.json"
    )
    raw = request_path.read_bytes()
    request = json.loads(raw)
    request_body = {key: value for key, value in request.items() if key != "request_id"}
    assert hashlib.sha256(canonical_json(request_body).encode("utf-8")).hexdigest() == (
        request_id
    )
    assert request["targets"] == [contracts[0].id, contracts[1].id]
    assert request["previous_by_target"] == {
        contracts[0].id: first_head,
        contracts[1].id: second_head,
    }
    assert request["action"] == "set"
    assert request["worker_id"] == "worker:two"
    assert request["reason"] == "rotate after freeze"
    assert type(request["observed_revision"]) is int
    assert shlex.quote(str(workspace.resolve())) in result.stderr
    if os.name != "nt":
        assert request_path.stat().st_mode & 0o777 == 0o600
        assert request_path.parent.stat().st_mode & 0o777 == 0o700

    facts_before_later_head = _assignment_facts(database)
    assert len(facts_before_later_head) == 4
    later_head = rows[0]["data"]["record_id"]
    later = _json_result(
        runner,
        [
            "task",
            "reassign",
            contracts[0].id,
            "--clear",
            "--previous",
            later_head,
            "--reason",
            "clear after the frozen request",
            "--idempotency-key",
            "later-clear",
            "--json",
        ],
    )
    assert later["data"]["previous_assignment_id"] == later_head

    resumed = _json_result(
        runner, ["task", "reassign", "--resume", request_id, "--json"]
    )
    resumed_rows = resumed["data"]["results"]
    assert resumed["data"]["request_id"] == request_id
    assert [row["data"]["record_id"] for row in resumed_rows] == [
        row["data"]["record_id"] for row in rows
    ]
    assert [row["data"]["candidate_id"] for row in resumed_rows] == [
        row["data"]["candidate_id"] for row in rows
    ]
    assert [row["requested_previous_assignment_id"] for row in resumed_rows] == [
        first_head,
        second_head,
    ]
    assert len(_assignment_facts(database)) == 5


def test_current_prepares_all_targets_before_any_assignment_write(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    runner = CliRunner()
    _seed_assignment(runner, contracts[0].id, "valid")
    before = _assignment_facts(database)

    result = runner.invoke(
        _app(),
        [
            "task",
            "reassign",
            contracts[0].id,
            contracts[1].id,
            "--current",
            "--worker",
            "worker:two",
            "--reason",
            "one target has no head",
            "--json",
        ],
    )
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "assignment_head_missing"
    assert contracts[1].id in payload["error"]["message"]
    assert _assignment_facts(database) == before
    request_dir = tmp_path / ".aig" / "requests" / "reassign"
    assert not request_dir.exists()


def test_resume_rejects_overrides_before_opening_store(tmp_path, monkeypatch):
    _database, _contracts = _workspace(tmp_path, monkeypatch, count=1)
    import aigineering.cli.task_assignment as cli_assignment

    monkeypatch.setattr(
        cli_assignment,
        "_persistent_snapshot",
        lambda: pytest.fail("resume override must reject before Store access"),
    )
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "reassign",
            "some-contract",
            "--resume",
            "a" * 64,
            "--reason",
            "changed intent",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["ok"] is False


def test_resume_rejects_modified_request_without_publishing(tmp_path, monkeypatch):
    database, contracts = _workspace(tmp_path, monkeypatch, count=1)
    runner = CliRunner()
    _seed_assignment(runner, contracts[0].id, "tamper")
    created = runner.invoke(
        _app(),
        [
            "task",
            "reassign",
            contracts[0].id,
            "--current",
            "--worker",
            "worker:two",
            "--reason",
            "immutable reason",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    response = json.loads(created.stdout)
    request_id = response["data"]["request_id"]
    request_path = (
        tmp_path / ".aig" / "requests" / "reassign" / f"request-{request_id}.json"
    )
    request_path.write_bytes(
        request_path.read_bytes().replace(b"immutable reason", b"changed reason")
    )
    before = _assignment_facts(database)

    resumed = _json_result(
        runner,
        ["task", "reassign", "--resume", request_id, "--json"],
        expected=1,
    )
    assert resumed["ok"] is False
    assert _assignment_facts(database) == before


def test_current_clear_is_a_saved_head_for_a_later_current_set(tmp_path, monkeypatch):
    database, contracts = _workspace(tmp_path, monkeypatch, count=1)
    runner = CliRunner()
    original_head = _seed_assignment(runner, contracts[0].id, "clear")

    cleared = runner.invoke(
        _app(),
        [
            "task",
            "reassign",
            contracts[0].id,
            "--current",
            "--clear",
            "--reason",
            "clear current owner",
            "--json",
        ],
    )
    assert cleared.exit_code == 0, cleared.output
    clear_response = json.loads(cleared.stdout)
    clear_row = clear_response["data"]["results"][0]
    assert clear_row["requested_previous_assignment_id"] == original_head
    assert clear_row["data"]["worker_id"] == ""

    assigned = runner.invoke(
        _app(),
        [
            "task",
            "reassign",
            contracts[0].id,
            "--current",
            "--worker",
            "worker:two",
            "--reason",
            "set after clear",
            "--json",
        ],
    )
    assert assigned.exit_code == 0, assigned.output
    assign_response = json.loads(assigned.stdout)
    assert (
        assign_response["data"]["results"][0]["requested_previous_assignment_id"]
        == clear_row["data"]["record_id"]
    )
    assert assign_response["data"]["results"][0]["data"]["worker_id"] == "worker:two"
    assert len(_assignment_facts(database)) == 3


def test_current_multi_clear_freezes_each_target_head(tmp_path, monkeypatch):
    database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    runner = CliRunner()
    heads = [
        _seed_assignment(runner, contracts[0].id, "multi-clear-one"),
        _seed_assignment(runner, contracts[1].id, "multi-clear-two"),
    ]
    cleared = runner.invoke(
        _app(),
        [
            "task",
            "reassign",
            contracts[0].id,
            contracts[1].id,
            "--current",
            "--clear",
            "--reason",
            "clear the selected Workers",
            "--json",
        ],
    )
    assert cleared.exit_code == 0, cleared.output
    rows = json.loads(cleared.stdout)["data"]["results"]
    assert [row["requested_previous_assignment_id"] for row in rows] == heads
    assert [row["data"]["worker_id"] for row in rows] == ["", ""]
    assert len(_assignment_facts(database)) == 4


def test_resume_replays_exact_pending_member_and_continues_unattempted_targets(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=3)
    runner = CliRunner()
    original_heads = [
        _seed_assignment(runner, contract.id, f"pending-{index}")
        for index, contract in enumerate(contracts)
    ]
    import aigineering.task_assignment as app_assignment
    from aigineering.runtime_services import RedisSyncPending

    state = {"pending": False, "restored": False}

    def relay(_store, *, accepted=None):
        if accepted is True and not state["pending"]:
            state["pending"] = True
            raise RedisSyncPending(accepted=True)
        if state["pending"] and not state["restored"]:
            raise RedisSyncPending(accepted=accepted)

    monkeypatch.setattr(app_assignment, "sync_runtime", relay)
    attempted = runner.invoke(
        _app(),
        [
            "task",
            "reassign",
            *(contract.id for contract in contracts),
            "--current",
            "--worker",
            "worker:two",
            "--reason",
            "recover the same request",
            "--idempotency-key",
            "current-pending-base",
            "--json",
        ],
    )
    assert attempted.exit_code == 1
    pending = json.loads(attempted.stdout)
    request_id = pending["data"]["request_id"]
    rows = pending["data"]["results"]
    assert [row["outcome"] for row in rows] == [
        "redis_sync_pending",
        "not_attempted",
        "not_attempted",
    ]
    assert [row["requested_previous_assignment_id"] for row in rows] == original_heads
    assert rows[0]["candidate_accepted"] is True
    assert rows[0]["facts_retained"] is True
    assert rows[0]["data"]["previous_assignment_id"] == original_heads[0]
    pending_facts = _assignment_facts(database)
    assert len(pending_facts) == 4
    pending_ids = {fact.payload["contract_id"]: fact.id for fact in pending_facts}

    state["restored"] = True
    recovered = _json_result(
        runner, ["task", "reassign", "--resume", request_id, "--json"]
    )
    recovered_rows = recovered["data"]["results"]
    assert [row["outcome"] for row in recovered_rows] == [
        "accepted",
        "accepted",
        "accepted",
    ]
    assert all(row["request_id"] == request_id for row in recovered_rows)
    assert [row["requested_previous_assignment_id"] for row in recovered_rows] == (
        original_heads
    )
    recovered_facts = {
        fact.payload["contract_id"]: fact.id
        for fact in _assignment_facts(database)
        if fact.payload["reason"] == "recover the same request"
    }
    assert recovered_facts[contracts[0].id] == pending_ids[contracts[0].id]
    assert {
        row["contract_id"]: row["data"]["record_id"] for row in recovered_rows
    } == recovered_facts
    assert len(_assignment_facts(database)) == 6


def test_saved_request_survives_process_stop_after_durable_save(tmp_path, monkeypatch):
    database, contracts = _workspace(tmp_path, monkeypatch, count=1)
    runner = CliRunner()
    _seed_assignment(runner, contracts[0].id, "stop")
    before = _assignment_facts(database)
    import aigineering.cli.task_assignment as cli_assignment

    save_request = cli_assignment.save_assignment_request

    def stop_after_save(request):
        save_request(request)
        raise SystemExit(17)

    monkeypatch.setattr(cli_assignment, "save_assignment_request", stop_after_save)
    stopped = runner.invoke(
        _app(),
        [
            "task",
            "reassign",
            contracts[0].id,
            "--current",
            "--worker",
            "worker:two",
            "--reason",
            "stop after request save",
            "--json",
        ],
    )
    assert stopped.exit_code == 17
    assert _assignment_facts(database) == before
    request_files = tuple(
        (tmp_path / ".aig" / "requests" / "reassign").glob("request-*.json")
    )
    assert len(request_files) == 1
    request_id = json.loads(request_files[0].read_bytes())["request_id"]

    monkeypatch.setattr(cli_assignment, "save_assignment_request", save_request)
    resumed = _json_result(
        runner, ["task", "reassign", "--resume", request_id, "--json"]
    )
    assert resumed["data"]["request_id"] == request_id
    assert len(_assignment_facts(database)) == 2


def test_final_directory_fsync_failure_leaves_no_assignment_and_can_resume(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=1)
    runner = CliRunner()
    _seed_assignment(runner, contracts[0].id, "fsync")
    before = _assignment_facts(database)
    import aigineering.task_assignment_requests as requests

    fsync_directory = requests._fsync_directory
    calls = 0

    def fail_final_directory_fsync(path):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("private fsync failure marker")
        fsync_directory(path)

    monkeypatch.setattr(requests, "_fsync_directory", fail_final_directory_fsync)
    failed = runner.invoke(
        _app(),
        [
            "task",
            "reassign",
            contracts[0].id,
            "--current",
            "--worker",
            "worker:two",
            "--reason",
            "recover after save durability failure",
            "--json",
        ],
    )
    assert failed.exit_code == 1
    assert json.loads(failed.stdout)["error"]["code"] == "request_storage_failed"
    assert _assignment_facts(database) == before
    request_files = tuple(
        (tmp_path / ".aig" / "requests" / "reassign").glob("request-*.json")
    )
    assert len(request_files) == 1
    request_id = json.loads(request_files[0].read_bytes())["request_id"]

    monkeypatch.setattr(requests, "_fsync_directory", fsync_directory)
    resumed = _json_result(
        runner, ["task", "reassign", "--resume", request_id, "--json"]
    )
    assert resumed["data"]["request_id"] == request_id
    assert len(_assignment_facts(database)) == 2


def test_request_temp_name_collision_preserves_unowned_file_and_identity(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=1)
    runner = CliRunner()
    _seed_assignment(runner, contracts[0].id, "temp-collision")
    facts_before = _assignment_facts(database)
    root_key = database.parent / "identity" / "root.ed25519"
    identity_before = root_key.read_bytes()

    from aigineering.task_assignment_requests import (
        AssignmentRequestError,
        prepare_assignment_request,
        save_assignment_request,
    )
    import aigineering.task_assignment_requests as requests

    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        request = prepare_assignment_request(
            snapshot,
            store_path=database,
            targets=(contracts[0].id,),
            worker_id="worker:two",
            reason="preserve colliding temp file",
            batch_base="collision-batch-base",
        )
    finally:
        snapshot.close()

    request_dir = database.parent / "requests" / "reassign"
    request_dir.mkdir(parents=True, mode=0o700)
    if os.name != "nt":
        request_dir.parent.chmod(0o700)
        request_dir.chmod(0o700)
    collision = request_dir / ".tmp-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    sentinel = b"pre-existing request temp; not owned by this save"
    collision.write_bytes(sentinel)
    if os.name != "nt":
        collision.chmod(0o600)
    monkeypatch.setattr(requests.secrets, "token_hex", lambda _size: "a" * 32)

    with pytest.raises(AssignmentRequestError) as error:
        save_assignment_request(request)

    assert error.value.code == "request_storage_failed"
    assert collision.read_bytes() == sentinel
    final_path = request_dir / f"request-{request['request_id']}.json"
    assert not final_path.exists()
    assert _assignment_facts(database) == facts_before
    assert root_key.read_bytes() == identity_before


def test_current_mode_conflicts_and_missing_reason_reject_before_store(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch, count=1)
    before = _assignment_facts(database)
    import aigineering.cli.task_assignment as cli_assignment

    monkeypatch.setattr(
        cli_assignment,
        "_persistent_snapshot",
        lambda: pytest.fail("invalid fresh request must reject before Store access"),
    )
    conflict = CliRunner().invoke(
        _app(),
        [
            "task",
            "reassign",
            contracts[0].id,
            "--current",
            "--worker",
            "worker:two",
            "--previous",
            "old-head",
            "--reason",
            "conflicting mode",
            "--json",
        ],
    )
    assert conflict.exit_code == 1
    assert json.loads(conflict.stdout)["error"]["code"] == "invalid_input"
    missing_reason = CliRunner().invoke(
        _app(),
        [
            "task",
            "reassign",
            contracts[0].id,
            "--current",
            "--worker",
            "worker:two",
            "--json",
        ],
    )
    assert missing_reason.exit_code == 2
    assert "Missing option '--reason'" in missing_reason.output
    assert _assignment_facts(database) == before


@pytest.mark.parametrize("kind", ["fifo", "deep-json", "symlink"])
def test_resume_bounds_special_files_and_parser_depth_before_publication(
    tmp_path, monkeypatch, kind
):
    database, _contracts = _workspace(tmp_path, monkeypatch, count=1)
    before = _assignment_facts(database)
    request_dir = tmp_path / ".aig" / "requests" / "reassign"
    request_dir.mkdir(parents=True, mode=0o700)
    if os.name != "nt":
        request_dir.parent.chmod(0o700)
        request_dir.chmod(0o700)
    request_id = "a" * 64
    request_path = request_dir / f"request-{request_id}.json"
    if kind == "fifo":
        if os.name == "nt":
            pytest.skip("FIFO is not available on this platform")
        os.mkfifo(request_path, 0o600)
    elif kind == "symlink":
        if os.name == "nt":
            pytest.skip("Symlink creation is not available on this platform")
        target_path = tmp_path / "untrusted-request-target.json"
        target_path.write_text("{}", encoding="utf-8")
        target_path.chmod(0o600)
        request_path.symlink_to(target_path)
    else:
        request_path.write_bytes(b"[" * 2048 + b"]" * 2048)
        if os.name != "nt":
            request_path.chmod(0o600)

    result = CliRunner().invoke(
        _app(),
        ["task", "reassign", "--resume", request_id, "--json"],
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "request_invalid"
    assert _assignment_facts(database) == before


def test_request_is_bound_to_resolved_store_path_and_genesis(tmp_path, monkeypatch):
    database, contracts = _workspace(tmp_path / "origin", monkeypatch, count=1)
    runner = CliRunner()
    _seed_assignment(runner, contracts[0].id, "binding")
    created = runner.invoke(
        _app(),
        [
            "task",
            "reassign",
            contracts[0].id,
            "--current",
            "--worker",
            "worker:two",
            "--reason",
            "workspace binding",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    request_id = json.loads(created.stdout)["data"]["request_id"]
    request_path = (
        database.parent / "requests" / "reassign" / f"request-{request_id}.json"
    )

    import aigineering.cli.task_assignment as cli_assignment
    from aigineering.task_assignment_requests import (
        AssignmentRequestError,
        load_assignment_request,
        validate_assignment_request_domain,
    )

    request = load_assignment_request(database, request_id)
    clone = tmp_path / "same-domain-clone"
    clone_store = clone / ".aig" / "store.db"
    clone_store.parent.mkdir(parents=True)
    shutil.copy2(database, clone_store)
    clone_requests = clone_store.parent / "requests" / "reassign"
    clone_requests.mkdir(parents=True, mode=0o700)
    if os.name != "nt":
        clone_requests.parent.chmod(0o700)
        clone_requests.chmod(0o700)
    clone_path = clone_requests / request_path.name
    shutil.copyfile(request_path, clone_path)
    if os.name != "nt":
        clone_path.chmod(0o600)
    monkeypatch.chdir(clone)
    monkeypatch.setattr(
        cli_assignment,
        "_persistent_snapshot",
        lambda: pytest.fail("wrong-path request must reject before Store access"),
    )
    wrong_path = CliRunner().invoke(
        _app(), ["task", "reassign", "--resume", request_id, "--json"]
    )
    assert wrong_path.exit_code == 1
    assert (
        json.loads(wrong_path.stdout)["error"]["code"] == "request_workspace_mismatch"
    )

    other_database, _other_contracts = _workspace(
        tmp_path / "different-domain", monkeypatch, count=1
    )
    other = SQLiteStore.snapshot_from_path(other_database)
    try:
        with pytest.raises(AssignmentRequestError) as error:
            validate_assignment_request_domain(request, other)
        assert error.value.code == "request_domain_mismatch"
    finally:
        other.close()
