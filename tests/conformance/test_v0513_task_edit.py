"""Consume the language-neutral signed task-edit vector and projection."""

from __future__ import annotations

import json
from pathlib import Path

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.handoff import project_task_handoff_effect
from aigineering.protocol.candidate import (
    candidate_proposal_from_dict,
    candidate_received_record,
    candidate_signing_bytes,
    genesis_manifest_from_dict,
    verify_candidate_proposal,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.wire import contract_from_dict, contract_to_dict


def test_public_task_edit_vector():
    vector = json.loads(
        Path("conformance/v0.5.13/task-edit-vectors.json").read_text(encoding="utf-8")
    )
    genesis = genesis_manifest_from_dict(vector["genesis"])
    source = contract_from_dict(vector["source_contract"])
    candidate_value = vector["candidate"]
    candidate = candidate_proposal_from_dict(candidate_value["value"])
    verify_candidate_proposal(candidate, genesis)
    assert (
        candidate_signing_bytes(candidate).decode("utf-8")
        == candidate_value["expected_signing_utf8"]
    )
    assert deep_thaw(candidate.effects[0].payload["edit"]) == vector["edit"]
    source_declaration = vector["source_declaration"]
    declaration = create_runtime_record(
        source_declaration["record_type"],
        source_declaration["payload"],
        causal_parents=source_declaration["causal_parents"],
        schema_version=source_declaration["schema_version"],
    )
    assert declaration.id == source_declaration["id"]
    receipt = candidate_received_record(candidate, genesis)
    assert deep_thaw(receipt.payload) == vector["receipt"]["payload"]
    assert receipt.id == vector["receipt"]["id"]
    projection = project_task_handoff_effect(
        candidate.effects[0],
        candidate,
        receipt.id,
        EffectProjectionContext(
            contracts=(source,),
            runtime_records=(declaration,),
            actor_keys=genesis.root_keys,
        ),
    )
    assert deep_thaw(contract_to_dict(projection.contract)) == vector["successor"]
    records = [
        {
            "id": record.id,
            "record_type": record.record_type,
            "schema_version": record.schema_version,
            "payload": deep_thaw(record.payload),
            "causal_parents": list(record.causal_parents),
        }
        for record in projection.records
    ]
    assert records == vector["records"]
