"""Preserve v0.5.13 signed fixture identity and expected fact bytes only."""

from __future__ import annotations

import json
from pathlib import Path

from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import (
    candidate_content_id,
    candidate_proposal_from_dict,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.wire import contract_from_dict

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = (
    "task-assignment-vectors.json",
    "worker-profile-promotion-vectors.json",
    "expanded-handoff-vectors.json",
    "task-handoff-vectors.json",
    "task-edit-vectors.json",
    "task-priority-vectors.json",
    "task-revision-vectors.json",
    "task-hold-vectors.json",
    "task-dependency-vectors.json",
    "task-promotion-vectors.json",
    "task-comment-vectors.json",
)


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _signed_candidate_vectors(document):
    for value in _walk(document):
        if (
            isinstance(value.get("value"), dict)
            and isinstance(value.get("expected_signing_utf8"), str)
            and "signature" in value["value"]
        ):
            yield value


def _expected_records(document):
    for value in _walk(document):
        if (
            isinstance(value.get("id"), str)
            and isinstance(value.get("record_type"), str)
            and isinstance(value.get("payload"), dict)
            and isinstance(value.get("causal_parents"), list)
        ):
            yield (
                create_runtime_record(
                    value["record_type"],
                    value["payload"],
                    causal_parents=value["causal_parents"],
                    schema_version=value.get("schema_version", 1),
                    recorded_at=value.get("recorded_at"),
                ),
                value["id"],
            )


def test_signed_fixture_identity_and_stored_expected_facts_are_read_only(tmp_path):
    for filename in FIXTURES:
        document = json.loads(
            (ROOT / "conformance" / "v0.5.13" / filename).read_text(encoding="utf-8")
        )
        genesis = genesis_manifest_from_dict(document["genesis"])
        candidates = tuple(_signed_candidate_vectors(document))
        assert candidates, filename
        seen_candidate_ids = set()
        for vector in candidates:
            candidate = candidate_proposal_from_dict(vector["value"])
            verify_candidate_proposal(candidate, genesis)
            assert candidate_content_id(candidate) == candidate.id
            assert (
                candidate_signing_bytes(candidate).decode("utf-8")
                == vector["expected_signing_utf8"]
            )
            seen_candidate_ids.add(candidate.id)

        records = tuple(_expected_records(document))
        unique = {}
        for record, expected_id in records:
            assert record.id == expected_id
            unique[record.id] = record
        if not unique:
            continue

        store = SQLiteStore(tmp_path / f"{filename}.sqlite")
        genesis_record = create_runtime_record(
            "domain.genesis", {"manifest": document["genesis"]}
        )
        store.append_runtime_record(genesis_record)
        # Registration facts are authenticated by the separate Worker
        # registration owner. Their canonical expected bytes are checked above,
        # but this generic fact fixture does not recreate that owner.
        stored = {
            record_id: record
            for record_id, record in unique.items()
            if record.record_type not in {"worker.registered", "lifecycle.terminal"}
        }
        # Test fixture import restores the recorded snapshot directly; it does
        # not call Candidate reducers or run feature projectors.
        with store._conn:
            for record in stored.values():
                if record.record_type == "contract.declared":
                    store._insert_contract(
                        contract_from_dict(deep_thaw(record.payload["contract"]))
                    )
            for record in stored.values():
                store._insert_runtime_record(record)
        before = tuple(
            (revision, record.id, record.record_type, deep_thaw(record.payload))
            for revision, record in store.scan_runtime_records()
        )
        after = tuple(
            (revision, record.id, record.record_type, deep_thaw(record.payload))
            for revision, record in store.scan_runtime_records()
        )
        assert after == before
        assert {row[1] for row in after} == {genesis_record.id, *stored}
        store.close()
