"""Verify installed registration identity coverage in the materialization digest.

This fixture publishes a normal signed local Worker registration without
invoking the Worker. It then corrupts one projection field at a time in private
SQLite fixtures; those mutations are negative tests, never operational facts.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid


_INSTALLED_BINDING = r"""
import hashlib, json, sys
from pathlib import Path
import aigineering
prefix = Path(sys.prefix).resolve()
module = Path(aigineering.__file__).resolve()
source_root = Path(sys.argv[1]).resolve()
inside_prefix = prefix == module or prefix in module.parents
inside_source = module == source_root or source_root in module.parents
if not inside_prefix or inside_source:
    raise RuntimeError("Python did not import the external installed package")
print(json.dumps({"installed": True,
                  "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
                  "module_under_prefix": inside_prefix,
                  "module_outside_source": not inside_source}))
"""

_SIGNED_PROJECTION_CASE = r"""
import hashlib, json, sys
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.integrity import check_store_integrity
from aigineering.local_identity import ensure_local_domain, ensure_local_worker_host
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.runtime_record import runtime_record_effective_payload

db_path, worker_id, field = sys.argv[1:]
if field not in {"actor_id", "key_id"}:
    raise ValueError("invalid projection field")

class RegistrationOnlyWorker:
    def __init__(self, selected_id):
        self.worker_id = selected_id
    def registration(self):
        return WorkerRegistration(
            self.worker_id, capabilities=("fixture-capability",),
            profile_id="registration-digest-fixture", capacity=1,
        )
    def invoke(self, contract, disclosed_assets):
        raise AssertionError("identity digest fixture must never invoke a Worker")

def fact_snapshot(store):
    entries = []
    registration_ids = []
    for revision, record in store.scan_runtime_records():
        entries.append({
            "revision": revision,
            "record_id": record.id,
            "recorded_at": record.recorded_at,
            **runtime_record_effective_payload(record),
        })
        if (record.record_type == "worker.registered"
                and record.payload.get("worker_id") == worker_id):
            registration_ids.append(record.id)
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), registration_ids, len(entries)

def table_snapshot(store, *, ignore_registration_updated_at=False):
    names = [row[0] for row in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    )]
    table_hashes = {}
    for name in names:
        escaped = '"' + name.replace('"', '""') + '"'
        rows = []
        cursor = store._conn.execute(f"SELECT * FROM {escaped}")
        columns = [item[0] for item in cursor.description]
        for row in cursor.fetchall():
            values = []
            selected = list(zip(columns, tuple(row)))
            if ignore_registration_updated_at and name == "worker_registrations":
                selected = [(column, value) for column, value in selected if column != "updated_at"]
            for _, value in selected:
                if isinstance(value, bytes):
                    values.append({"bytes_sha256": hashlib.sha256(value).hexdigest()})
                else:
                    values.append(value)
            rows.append(json.dumps(values, sort_keys=True, separators=(",", ":")))
        schema = store._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()[0]
        encoded = json.dumps(
            {"schema": schema, "rows": sorted(rows)},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        table_hashes[name] = hashlib.sha256(encoded).hexdigest()
    aggregate = json.dumps(table_hashes, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(aggregate).hexdigest(), table_hashes

def checked_integrity(store):
    before_hash, before_tables = table_snapshot(store)
    report = check_store_integrity(store)
    after_hash, after_tables = table_snapshot(store)
    if before_hash != after_hash or before_tables != after_tables:
        raise RuntimeError("integrity consumer changed original SQLite tables")
    return report, before_hash, len(before_tables)

store = SQLiteStore(db_path)
try:
    ensure_local_domain(store)
    ensure_local_worker_host(store, RegistrationOnlyWorker(worker_id))
    registration = store.get_worker_registration(worker_id)
    if registration is None:
        raise RuntimeError("signed Worker registration was not materialized")
    original_actor = registration.actor_id
    original_key = registration.key_id
    if original_actor != worker_id or not original_key:
        raise RuntimeError("signed registration identity is incomplete")
    digest_before_canonical_rebuild = store.runtime_materialization_digest()
    facts_before_canonical_rebuild, ids_before_canonical_rebuild, count_before_canonical_rebuild = fact_snapshot(store)
    raw_before_canonical_rebuild, _ = table_snapshot(store)
    semantic_before_canonical_rebuild, _ = table_snapshot(
        store, ignore_registration_updated_at=True
    )
    canonical_rebuild_digest = store.rebuild_runtime_materializations()
    baseline_digest = store.runtime_materialization_digest()
    baseline_facts, registration_ids, fact_count = fact_snapshot(store)
    raw_after_canonical_rebuild, _ = table_snapshot(store)
    baseline_semantic_hash, _ = table_snapshot(
        store, ignore_registration_updated_at=True
    )
    baseline_normal_rebuild = {
        "digest_stable": (
            digest_before_canonical_rebuild == canonical_rebuild_digest == baseline_digest
        ),
        "facts_stable": (
            facts_before_canonical_rebuild == baseline_facts
            and ids_before_canonical_rebuild == registration_ids
            and count_before_canonical_rebuild == fact_count
        ),
        "semantic_tables_stable": semantic_before_canonical_rebuild == baseline_semantic_hash,
        "raw_tables_changed": raw_before_canonical_rebuild != raw_after_canonical_rebuild,
        "raw_tables_before_sha256": raw_before_canonical_rebuild,
        "raw_tables_after_sha256": raw_after_canonical_rebuild,
    }
    if not all(
        baseline_normal_rebuild[key]
        for key in ("digest_stable", "facts_stable", "semantic_tables_stable")
    ):
        raise RuntimeError("baseline materialization rebuild changed semantic state")
    baseline_integrity, baseline_table_hash, table_count = checked_integrity(store)
    if baseline_integrity.get("consistent") is not True:
        raise RuntimeError("signed registration baseline failed local integrity check")
    if len(registration_ids) != 1:
        raise RuntimeError("fixture did not create exactly one signed registration fact")
    before_row = dict(store._conn.execute(
        "SELECT * FROM worker_registrations WHERE worker_id = ?", (worker_id,)
    ).fetchone())
    replacement = before_row[field] + ":projection-only-test"
    cursor = store._conn.execute(
        f"UPDATE worker_registrations SET {field} = ? WHERE worker_id = ?",
        (replacement, worker_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("negative projection fixture did not update one row")
    store._conn.commit()
    after_row = dict(store._conn.execute(
        "SELECT * FROM worker_registrations WHERE worker_id = ?", (worker_id,)
    ).fetchone())
    expected_row = dict(before_row)
    expected_row[field] = replacement
    if after_row != expected_row:
        raise RuntimeError("negative fixture changed more than its selected column")
    mutated_digest = store.runtime_materialization_digest()
    mutated_semantic_hash, _ = table_snapshot(
        store, ignore_registration_updated_at=True
    )
    mutated_integrity, mutated_check_table_hash, mutated_table_count = checked_integrity(store)
    mutated_facts, mutated_registration_ids, mutated_fact_count = fact_snapshot(store)
    after_mutation_registration = store.get_worker_registration(worker_id)
    if after_mutation_registration is None:
        raise RuntimeError("projection corruption removed the registration")
    other_field = "key_id" if field == "actor_id" else "actor_id"
    if getattr(after_mutation_registration, other_field) != (
        original_key if other_field == "key_id" else original_actor
    ):
        raise RuntimeError("negative fixture changed the other identity field")
    if mutated_digest == baseline_digest:
        raise RuntimeError("materialization digest missed projected identity corruption")
    if mutated_integrity.get("consistent") is not False:
        raise RuntimeError("local integrity consumer missed projected identity corruption")
    if (mutated_facts != baseline_facts
            or mutated_registration_ids != registration_ids
            or mutated_fact_count != fact_count):
        raise RuntimeError("projection-only negative changed immutable RuntimeRecords")

    rebuilt_digest = store.rebuild_runtime_materializations()
    rebuilt_semantic_hash, _ = table_snapshot(
        store, ignore_registration_updated_at=True
    )
    rebuilt_integrity, rebuilt_check_table_hash, rebuilt_table_count = checked_integrity(store)
    restored = store.get_worker_registration(worker_id)
    restored_facts, restored_ids, restored_count = fact_snapshot(store)
    if restored is None or (restored.actor_id, restored.key_id) != (
        original_actor, original_key
    ):
        raise RuntimeError("normal rebuild did not restore signed registration identity")
    if rebuilt_integrity.get("consistent") is not True:
        raise RuntimeError("local integrity consumer rejected rebuilt projection")
    if (rebuilt_digest != baseline_digest
            or store.runtime_materialization_digest() != baseline_digest
            or restored_facts != baseline_facts
            or restored_ids != registration_ids
            or restored_count != fact_count):
        raise RuntimeError("rebuild did not restore the original digest and facts")
finally:
    store.close()

store = SQLiteStore(db_path)
try:
    reopened = store.get_worker_registration(worker_id)
    reopened_facts, reopened_ids, reopened_count = fact_snapshot(store)
    reopened_digest = store.runtime_materialization_digest()
    reopened_semantic_hash, _ = table_snapshot(
        store, ignore_registration_updated_at=True
    )
    reopened_integrity, reopened_check_table_hash, reopened_table_count = checked_integrity(store)
    if reopened is None or (reopened.actor_id, reopened.key_id) != (
        original_actor, original_key
    ):
        raise RuntimeError("Store reopen did not restore signed registration identity")
    if reopened_integrity.get("consistent") is not True:
        raise RuntimeError("local integrity consumer rejected reopened projection")
    if (reopened_digest != baseline_digest or reopened_facts != baseline_facts
            or reopened_ids != registration_ids or reopened_count != fact_count):
        raise RuntimeError("Store reopen changed original digest or immutable facts")
    if not (
        mutated_semantic_hash != baseline_semantic_hash
        and rebuilt_semantic_hash == baseline_semantic_hash
        and reopened_semantic_hash == baseline_semantic_hash
    ):
        raise RuntimeError("rebuild or reopen changed semantic projection columns")
finally:
    store.close()

print(json.dumps({
    "status": "passed",
    "worker_id": worker_id,
    "field": field,
    "baseline_digest": baseline_digest,
    "mutated_digest": mutated_digest,
    "rebuilt_digest": rebuilt_digest,
    "reopened_digest": reopened_digest,
    "baseline_normal_rebuild": baseline_normal_rebuild,
    "facts_sha256": baseline_facts,
    "mutated_facts_sha256": mutated_facts,
    "integrity_consumer": {
        "baseline_consistent": baseline_integrity["consistent"],
        "mutated_consistent": mutated_integrity["consistent"],
        "rebuilt_consistent": rebuilt_integrity["consistent"],
        "reopened_consistent": reopened_integrity["consistent"],
        "read_only_checks_preserved_all_tables": True,
        "original_store_table_count": table_count,
        "original_store_table_hashes": [
            baseline_table_hash,
            mutated_check_table_hash,
            rebuilt_check_table_hash,
            reopened_check_table_hash,
        ],
        "table_counts_at_checks": [
            table_count, mutated_table_count, rebuilt_table_count, reopened_table_count
        ],
        "semantic_table_hashes": [
            baseline_semantic_hash,
            mutated_semantic_hash,
            rebuilt_semantic_hash,
            reopened_semantic_hash,
        ],
    },
    "registration_fact_ids": registration_ids,
    "other_identity_field_unchanged": True,
    "only_selected_projection_column_changed": True,
    "worker_invocations": 0,
}, sort_keys=True))
"""


def _exclusive(path: Path, data: str | bytes) -> None:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def installed_registration_digest_smoke(
    aig: Path,
    python: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict:
    """Exercise signed registration projection identity in an installed wheel."""
    if not aig.is_file() or not python.is_file():
        raise RuntimeError("installed CLI and Python paths must be regular files")
    redis_url = env.get("AIGINEERING_REDIS_URL", "").strip()
    if not redis_url or not redis_url.rsplit("/", 1)[-1].isdigit():
        raise RuntimeError("an explicit isolated Redis database URL is required")

    evidence = cwd / f"registration-digest-{uuid.uuid4().hex}"
    evidence.mkdir(mode=0o700)
    commands: list[dict] = []
    phase = "installed-python-binding"
    source_root = Path(__file__).resolve().parents[1] / "src"
    local_env = dict(env)
    local_env["AIGINEERING_REDIS_URL"] = redis_url
    for name in (
        "AIGINEERING_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "PYTHONPATH",
        "PYTHONHOME",
    ):
        local_env.pop(name, None)

    def retain(label: str, argv: list[str], completed) -> None:
        index = len(commands) + 1
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8", errors="surrogateescape")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8", errors="surrogateescape")
        out_path = evidence / f"{index:02d}-{label}-stdout.raw"
        err_path = evidence / f"{index:02d}-{label}-stderr.raw"
        _exclusive(out_path, stdout)
        _exclusive(err_path, stderr)
        commands.append(
            {
                "phase": label,
                "argv": argv,
                "exit_code": completed.returncode,
                "stdout_file": out_path.name,
                "stderr_file": err_path.name,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
            }
        )
        if completed.returncode != 0:
            raise RuntimeError(f"{label} failed with exit {completed.returncode}")

    def run(
        label: str,
        argv: list[str],
        timeout: float = 90,
        selected_env: dict[str, str] | None = None,
    ) -> dict:
        nonlocal phase
        phase = label
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=selected_env if selected_env is not None else local_env,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            completed = subprocess.CompletedProcess(
                argv, -signal.SIGKILL, exc.stdout or b"", exc.stderr or b""
            )
            retain(label + "-timeout", argv, completed)
            raise RuntimeError(f"{label} exceeded its bounded timeout") from None
        retain(label, argv, completed)
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"{label} did not return one JSON object") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{label} returned an invalid result shape")
        return payload

    try:
        binding = run(
            "installed-python-binding",
            [str(python), "-c", _INSTALLED_BINDING, str(source_root)],
        )
        if (
            binding.get("installed") is not True
            or binding.get("module_under_prefix") is not True
            or binding.get("module_outside_source") is not True
        ):
            raise RuntimeError("fixture Python did not bind to the installed wheel")

        results = {}
        for field in ("actor_id", "key_id"):
            case_root = evidence / field
            identity = case_root / "identity"
            identity.mkdir(parents=True, mode=0o700)
            db_path = case_root / "store.sqlite"
            worker_id = f"worker:registration-digest-{field}"
            case_env = dict(local_env)
            case_env["AIG_ACTOR_KEY_FILE"] = str(identity / "root.ed25519")
            argv = [
                str(python),
                "-c",
                _SIGNED_PROJECTION_CASE,
                str(db_path),
                worker_id,
                field,
            ]
            result = run(
                f"signed-{field}-projection-control",
                argv,
                timeout=120,
                selected_env=case_env,
            )
            integrity = result.get("integrity_consumer")
            baseline_rebuild = result.get("baseline_normal_rebuild")
            raw_table_hashes = (
                integrity.get("original_store_table_hashes")
                if isinstance(integrity, dict)
                else None
            )
            semantic_table_hashes = (
                integrity.get("semantic_table_hashes")
                if isinstance(integrity, dict)
                else None
            )
            if (
                result.get("status") != "passed"
                or result.get("field") != field
                or not isinstance(result.get("baseline_digest"), str)
                or result.get("mutated_digest") == result.get("baseline_digest")
                or result.get("rebuilt_digest") != result.get("baseline_digest")
                or result.get("reopened_digest") != result.get("baseline_digest")
                or result.get("facts_sha256") != result.get("mutated_facts_sha256")
                or result.get("other_identity_field_unchanged") is not True
                or result.get("only_selected_projection_column_changed") is not True
                or result.get("worker_invocations") != 0
                or not isinstance(result.get("registration_fact_ids"), list)
                or len(result["registration_fact_ids"]) != 1
                or not isinstance(integrity, dict)
                or integrity.get("baseline_consistent") is not True
                or integrity.get("mutated_consistent") is not False
                or integrity.get("rebuilt_consistent") is not True
                or integrity.get("reopened_consistent") is not True
                or integrity.get("read_only_checks_preserved_all_tables") is not True
                or not isinstance(integrity.get("original_store_table_count"), int)
                or integrity.get("original_store_table_count", 0) <= 0
                or not isinstance(raw_table_hashes, list)
                or len(raw_table_hashes) != 4
                or any(
                    not isinstance(item, str) or len(item) != 64
                    for item in raw_table_hashes
                )
                or integrity.get("table_counts_at_checks")
                != [integrity.get("original_store_table_count")] * 4
                or not isinstance(semantic_table_hashes, list)
                or len(semantic_table_hashes) != 4
                or any(
                    not isinstance(item, str) or len(item) != 64
                    for item in semantic_table_hashes
                )
                or semantic_table_hashes[0] == semantic_table_hashes[1]
                or semantic_table_hashes[0] != semantic_table_hashes[2]
                or semantic_table_hashes[0] != semantic_table_hashes[3]
                or not isinstance(baseline_rebuild, dict)
                or baseline_rebuild.get("digest_stable") is not True
                or baseline_rebuild.get("facts_stable") is not True
                or baseline_rebuild.get("semantic_tables_stable") is not True
                or not isinstance(baseline_rebuild.get("raw_tables_before_sha256"), str)
                or not isinstance(baseline_rebuild.get("raw_tables_after_sha256"), str)
            ):
                raise RuntimeError(f"{field} projection verification failed")
            results[field] = {
                "baseline_digest": result["baseline_digest"],
                "mutated_digest": result["mutated_digest"],
                "rebuilt_digest": result["rebuilt_digest"],
                "reopened_digest": result["reopened_digest"],
                "facts_sha256": result["facts_sha256"],
                "baseline_normal_rebuild": baseline_rebuild,
                "registration_fact_ids": result["registration_fact_ids"],
                "integrity_consumer": integrity,
                "projection_only_mutation": True,
                "worker_invocations": 0,
            }
        _exclusive(
            evidence / "commands-final.json",
            json.dumps(commands, sort_keys=True, indent=2) + "\n",
        )
        return {
            "status": "passed",
            "installed_module_sha256": binding["module_sha256"],
            "fields": results,
            "signed_registration_owner": "ensure_local_worker_host",
            "provider_calls": 0,
            "worker_invocations": 0,
            "private_evidence_id": evidence.name,
        }
    except Exception as exc:
        _exclusive(
            evidence / "failure.json",
            json.dumps(
                {
                    "phase": phase,
                    "error_type": type(exc).__name__,
                    "commands": commands,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )
        _exclusive(
            evidence / "commands-final.json",
            json.dumps(commands, sort_keys=True, indent=2) + "\n",
        )
        raise
