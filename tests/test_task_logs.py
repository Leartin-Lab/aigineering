"""Bounded local Worker capture is auxiliary, scoped, and read-only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import pytest
from click import Group
from click.testing import CliRunner

from aigineering.cli.task_logs import register_task_log_command
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.task_logs import (
    CaptureWriter,
    TaskLogError,
    capture_path,
    current_observer,
    observer_scope,
    read_capture,
    store_log_root,
)
import aigineering.task_logs as task_logs_module
from aigineering.agent.llm import LLMWorker, ProviderError
from aigineering.protocol.types import Contract


def _binding():
    return {
        "domain_id": "domain:test-log",
        "contract_id": "task:test-log",
        "claim_id": "claim-test-log",
        "epoch": 2,
        "package_id": "pkg:" + "a" * 64,
        "worker_id": "worker:test-log",
        "grant_record_id": "record:grant",
        "watermark": 5,
    }


def test_writer_page_cursor_and_store_adjacent_root(tmp_path):
    database = tmp_path / "custom" / "runtime.sqlite"
    database.parent.mkdir()
    root = store_log_root(database)
    binding = _binding()
    writer = CaptureWriter(database, binding)
    assert writer.warning is None
    writer.emit("invocation.started")
    writer.emit(
        "provider.retry",
        status_code=503,
        attempt=1,
        max_retries=1,
        backoff_seconds=0,
    )
    writer.emit("candidate.returned")
    writer.emit("invocation.finished", outcome="returned", duration_ms=10)
    assert writer.close("returned") is None

    capture = capture_path(root, binding["domain_id"], binding["claim_id"])
    assert capture.is_file()
    assert capture.stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700

    first = read_capture(root, binding, limit=2)
    assert first["ok"] is True
    assert first["data"]["follow_status"] == "complete"
    assert [row["event"] for row in first["data"]["events"]] == [
        "invocation.started",
        "provider.retry",
    ]
    assert first["data"]["truncated"] is True
    token = first["data"]["next_cursor"]
    second = read_capture(root, binding, limit=2, cursor=token)
    assert [row["event"] for row in second["data"]["events"]] == [
        "candidate.returned",
        "invocation.finished",
    ]
    assert second["data"]["truncated"] is False

    with pytest.raises(TaskLogError, match="capture_missing"):
        read_capture(root, {**binding, "claim_id": "foreign"}, cursor=token)


def test_event_allowlist_rejects_raw_body_and_bool_integer(tmp_path):
    binding = _binding()
    writer = CaptureWriter(tmp_path / "store.sqlite", binding)
    writer.emit(
        "provider.retry", status_code=True, attempt=1, max_retries=1, backoff_seconds=0
    )
    assert writer.warning == "capture_unavailable"
    writer.close()
    raw = capture_path(
        store_log_root(tmp_path / "store.sqlite"),
        binding["domain_id"],
        binding["claim_id"],
    ).read_text()
    assert "true" not in raw

    second = CaptureWriter(
        tmp_path / "other.sqlite", {**binding, "claim_id": "claim-other"}
    )
    marker = "PRIVATE_RAW_PROVIDER_BODY"
    second.emit("provider.failed", status_code=503, retryable=False, raw_body=marker)
    assert second.warning == "capture_unavailable"
    second.close()
    second_raw = capture_path(
        store_log_root(tmp_path / "other.sqlite"), binding["domain_id"], "claim-other"
    ).read_text()
    assert marker not in second_raw


def test_cursor_binds_verified_prefix_and_rejects_tampering(tmp_path):
    root = store_log_root(tmp_path / "store.sqlite")
    binding = _binding()
    writer = CaptureWriter(tmp_path / "store.sqlite", binding)
    writer.emit("invocation.started")
    writer.emit("candidate.returned")
    writer.close()
    first = read_capture(root, binding, limit=1)
    token = first["data"]["next_cursor"]

    path = capture_path(root, binding["domain_id"], binding["claim_id"])
    content = path.read_bytes()
    path.write_bytes(content.replace(b"candidate.returned", b"candidate.returned!", 1))
    with pytest.raises(TaskLogError, match="capture_corrupt"):
        read_capture(root, binding, limit=1, cursor=token)


def test_recomputed_hash_does_not_make_invalid_header_or_bool_sequence_valid(tmp_path):
    root = store_log_root(tmp_path / "store.sqlite")
    binding = _binding()
    writer = CaptureWriter(tmp_path / "store.sqlite", binding)
    writer.emit("invocation.started")
    writer.close()
    path = capture_path(root, binding["domain_id"], binding["claim_id"])
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    lines[0]["created_at"] = "2026-10-07"
    lines[0].pop("hash")

    def canonical(value):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()

    lines[0]["hash"] = hashlib.sha256(canonical(lines[0])).hexdigest()
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in lines
        )
    )
    with pytest.raises(TaskLogError, match="capture_corrupt"):
        read_capture(root, binding)

    writer = CaptureWriter(
        tmp_path / "second.sqlite", {**binding, "claim_id": "claim-second"}
    )
    writer.emit("invocation.started")
    writer.close()
    second_root = store_log_root(tmp_path / "second.sqlite")
    second_path = capture_path(second_root, binding["domain_id"], "claim-second")
    rows = [json.loads(line) for line in second_path.read_text().splitlines()]
    rows[1]["sequence"] = True
    rows[1].pop("hash")
    rows[1]["hash"] = hashlib.sha256(canonical(rows[1])).hexdigest()
    second_path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        )
    )
    with pytest.raises(TaskLogError, match="capture_corrupt"):
        read_capture(second_root, {**binding, "claim_id": "claim-second"})


def test_partial_tail_and_bounded_growth_fail_closed(tmp_path, monkeypatch):
    root = store_log_root(tmp_path / "store.sqlite")
    binding = _binding()
    writer = CaptureWriter(tmp_path / "store.sqlite", binding)
    writer.emit("invocation.started")
    writer.close()
    path = capture_path(root, binding["domain_id"], binding["claim_id"])
    original = path.read_bytes()
    path.write_bytes(original[:-1])
    with pytest.raises(TaskLogError, match="capture_incomplete"):
        read_capture(root, binding)
    path.write_bytes(original)

    original_read = task_logs_module.os.read
    monkeypatch.setattr(
        task_logs_module.os,
        "read",
        lambda fd, count: b"x" * (task_logs_module._MAX_CAPTURE_BYTES + 1),
    )
    with pytest.raises(TaskLogError, match="capture_corrupt"):
        read_capture(root, binding)
    monkeypatch.setattr(task_logs_module.os, "read", original_read)


def test_symlink_spool_root_is_unavailable_without_writing_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "store" / "worker-logs"
    root.parent.mkdir()
    root.symlink_to(target, target_is_directory=True)
    writer = CaptureWriter(tmp_path / "store" / "runtime.sqlite", _binding())
    assert writer.warning == "capture_unavailable"
    writer.close()
    assert list(target.iterdir()) == []


def test_observer_is_invocation_scoped_and_not_shared_between_threads():
    class Observer:
        def __init__(self, name):
            self.name = name

        def emit(self, *_args, **_kwargs):
            return None

    first = Observer("one")
    second = Observer("two")
    assert current_observer() is None

    def check(observer):
        with observer_scope(observer):
            assert current_observer() is observer
            with observer_scope(None):
                assert current_observer() is None
            assert current_observer() is observer
        assert current_observer() is None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(check, (first, second)))
    assert current_observer() is None


def test_real_llm_retry_branch_emits_only_typed_safe_metadata(tmp_path):
    marker = "PRIVATE_PROVIDER_RESPONSE_MARKER"
    calls = 0

    def transport(_url, _headers, _payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError(503, marker)
        return {"choices": [{"message": {"content": "safe result"}}]}

    binding = _binding()
    writer = CaptureWriter(tmp_path / "store.sqlite", binding)
    worker = LLMWorker(
        model="fixture-model",
        transport=transport,
        max_retries=1,
        retry_backoff=0,
    )
    with observer_scope(writer):
        candidate = worker.invoke(Contract(id="task:fixture"), [])
    assert candidate.raw_output == "safe result"
    writer.close("returned")
    result = read_capture(store_log_root(tmp_path / "store.sqlite"), binding)
    retry = next(
        row for row in result["data"]["events"] if row["event"] == "provider.retry"
    )
    assert retry["fields"] == {
        "status_code": 503,
        "attempt": 1,
        "max_retries": 1,
        "backoff_seconds": 0.0,
    }
    assert marker not in json.dumps(result)


def test_event_and_file_caps_report_truncated_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(task_logs_module, "_MAX_CAPTURE_EVENTS", 3)
    binding = _binding()
    writer = CaptureWriter(tmp_path / "store.sqlite", binding)
    writer.emit("invocation.started")
    writer.emit("candidate.returned")
    writer.emit("provider.failed", status_code=503, retryable=True)
    writer.emit("invocation.finished", outcome="returned", duration_ms=1)
    assert writer.warning == "capture_truncated"
    writer.close()
    result = read_capture(store_log_root(tmp_path / "store.sqlite"), binding)
    assert result["data"]["follow_status"] == "truncated"
    assert result["data"]["warnings"] == ["capture_truncated"]


def test_zero_byte_os_write_stops_capture_without_spinning(tmp_path, monkeypatch):
    binding = _binding()
    writer = CaptureWriter(tmp_path / "store.sqlite", binding)
    monkeypatch.setattr(task_logs_module.os, "write", lambda *_args: 0)
    writer.emit("invocation.started")
    assert writer.warning == "capture_unavailable"
    writer.close()


def test_reader_permission_failure_is_unavailable_not_corrupt(tmp_path, monkeypatch):
    binding = _binding()
    writer = CaptureWriter(tmp_path / "store.sqlite", binding)
    writer.emit("invocation.started")
    writer.close()
    root = store_log_root(tmp_path / "store.sqlite")
    real_open = task_logs_module.os.open

    def denied(path, flags, *args, **kwargs):
        if Path(path).suffix == ".jsonl":
            raise PermissionError("private marker")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(task_logs_module.os, "open", denied)
    with pytest.raises(TaskLogError, match="capture_unavailable") as error:
        read_capture(root, binding)
    assert error.value.code == "capture_unavailable"


def test_cli_static_json_and_follow_emit_compact_jsonl(tmp_path, monkeypatch):
    database = tmp_path / "custom" / "runtime.sqlite"
    database.parent.mkdir()
    store = SQLiteStore(database)
    store.close()
    binding = _binding()
    writer = CaptureWriter(database, binding)
    writer.emit("invocation.started")
    writer.emit("candidate.returned")
    writer.emit("invocation.finished", outcome="returned", duration_ms=3)
    writer.close()

    class Collaboration:
        def sync(self):
            return {"ok": True}

    monkeypatch.setattr(
        "aigineering.cli.task_logs.collaboration_service",
        lambda _store: Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_logs.task_log_binding",
        lambda *_args: binding,
    )
    root = Group()
    task = Group("task")
    register_task_log_command(task)
    root.add_command(task)

    static = CliRunner().invoke(
        root,
        [
            "task",
            "logs",
            binding["contract_id"],
            "--claim",
            binding["claim_id"],
            "--store",
            str(database),
            "--json",
        ],
    )
    assert static.exit_code == 0, static.output
    static_data = json.loads(static.output)
    assert static_data["data"]["follow_status"] == "complete"
    assert len(static_data["data"]["events"]) == 3

    followed = CliRunner().invoke(
        root,
        [
            "task",
            "logs",
            binding["contract_id"],
            "--claim",
            binding["claim_id"],
            "--store",
            str(database),
            "--follow",
            "--timeout",
            "0.01",
            "--json",
        ],
    )
    assert followed.exit_code == 0, followed.output
    lines = [json.loads(line) for line in followed.output.splitlines()]
    assert [line["type"] for line in lines] == ["event", "event", "event", "end"]
    assert lines[-1]["status"] == "timeout"


def test_cli_json_errors_exit_nonzero_and_missing_follow_is_not_success(
    tmp_path, monkeypatch
):
    binding = _binding()

    class Collaboration:
        def sync(self):
            return {"ok": True}

    database = tmp_path / "runtime.sqlite"
    store = SQLiteStore(database)
    store.close()
    monkeypatch.setattr(
        "aigineering.cli.task_logs.collaboration_service",
        lambda _store: Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_logs.task_log_binding",
        lambda *_args: binding,
    )
    root = Group()
    task = Group("task")
    register_task_log_command(task)
    root.add_command(task)
    missing = CliRunner().invoke(
        root,
        [
            "task",
            "logs",
            binding["contract_id"],
            "--claim",
            binding["claim_id"],
            "--store",
            str(database),
            "--json",
        ],
    )
    assert missing.exit_code == 1
    assert json.loads(missing.output) == {"ok": False, "error": "capture_missing"}

    follow_missing = CliRunner().invoke(
        root,
        [
            "task",
            "logs",
            binding["contract_id"],
            "--claim",
            binding["claim_id"],
            "--store",
            str(database),
            "--follow",
            "--timeout",
            "0.001",
            "--json",
        ],
    )
    assert follow_missing.exit_code == 1
    assert json.loads(follow_missing.output) == {
        "ok": False,
        "error": "capture_missing",
        "type": "end",
        "status": "error",
    }


def test_provider_meter_events_accept_exact_schema_and_coupled_values():
    epoch = "a" * 32
    admitted = {
        "meter_epoch_id": epoch,
        "permit_id": 1,
        "output_cap": 8192,
        "reserved_output_tokens": 8192,
        "timeout_seconds": 60.0,
    }
    entered = {"meter_epoch_id": epoch, "permit_id": 1}
    observed = {
        "meter_epoch_id": epoch,
        "permit_id": 1,
        "status_code": 200,
        "response_bytes": 32,
        "decode_status": "root_object",
        "usage_member_state": "value",
    }
    finalized = {
        "meter_epoch_id": epoch,
        "permit_id": 1,
        "outcome": "response",
        "status_code": 200,
        "error_code": None,
        "duration_seconds": 0.25,
        "late": False,
        "result_eligible": True,
        "prompt_tokens": 10,
        "prompt_status": "reported",
        "completion_tokens": 5,
        "completion_status": "reported",
        "total_tokens": 15,
        "total_status": "reported",
        "inconsistent_total": False,
        "output_cap_violation": False,
        "cached_reasoning_status": "unsupported",
    }
    for name, fields in (
        ("provider.permit_admitted", admitted),
        ("provider.transport_entered", entered),
        ("provider.response_observed", observed),
        ("provider.attempt_finalized", finalized),
    ):
        task_logs_module._validate_event(name, fields)


@pytest.mark.parametrize(
    ("event", "fields"),
    [
        (
            "provider.permit_admitted",
            {
                "meter_epoch_id": "a" * 32,
                "permit_id": True,
                "output_cap": 8192,
                "reserved_output_tokens": 8192,
                "timeout_seconds": 60,
            },
        ),
        (
            "provider.response_observed",
            {
                "meter_epoch_id": "a" * 32,
                "permit_id": 1,
                "status_code": 200,
                "response_bytes": 10,
                "decode_status": "invalid_json",
                "usage_member_state": "value",
            },
        ),
        (
            "provider.attempt_finalized",
            {
                "meter_epoch_id": "a" * 32,
                "permit_id": 1,
                "outcome": "response",
                "status_code": 503,
                "error_code": "http_status",
                "duration_seconds": 0.0,
                "late": False,
                "result_eligible": True,
                "prompt_tokens": 0,
                "prompt_status": "reported",
                "completion_tokens": None,
                "completion_status": "missing",
                "total_tokens": None,
                "total_status": "missing",
                "inconsistent_total": False,
                "output_cap_violation": None,
                "cached_reasoning_status": "unsupported",
            },
        ),
    ],
)
def test_provider_meter_event_schema_rejects_bad_coupling(event, fields):
    with pytest.raises(ValueError):
        task_logs_module._validate_event(event, fields)
