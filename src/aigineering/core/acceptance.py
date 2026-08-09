"""Independent output qualification and terminal consequence reduction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.ids import acceptance_policy_id
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.trace import create_entry
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.types import TraceEntry
from aigineering.protocol.wire import trace_entry_to_dict


@dataclass(frozen=True)
class AttestationProjection:
    records: tuple[RuntimeRecord, ...]
    relation_target: str
    additional_capabilities: tuple[str, ...]


class OutputQualificationConflict(ValueError):
    """A Contract output slot already selected another immutable Asset."""


def project_asset_attestation_records(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> AttestationProjection:
    """Validate an independent verifier and bind it to one exact output Asset."""
    contract_id = str(effect.payload.get("contract_id", ""))
    output_name = str(effect.payload.get("output_name", ""))
    asset_id = str(effect.payload.get("asset_id", ""))
    supplied_policy_id = str(effect.payload.get("policy_id", ""))
    supplied_policy_version = str(effect.payload.get("policy_version", ""))
    verdict = str(effect.payload.get("verdict", ""))
    rubric_value = effect.payload.get("rubric_asset_ids", ())
    evidence_value = effect.payload.get("evidence_asset_ids", ())
    if not contract_id or not output_name or not asset_id:
        raise ValueError("asset.attest requires contract_id, output_name, and asset_id")
    if verdict not in {"accepted", "rejected"}:
        raise ValueError("asset.attest verdict must be 'accepted' or 'rejected'")
    if not isinstance(rubric_value, (list, tuple)) or not all(
        isinstance(value, str) and value for value in rubric_value
    ):
        raise ValueError("asset.attest rubric_asset_ids must be strings")
    if not isinstance(evidence_value, (list, tuple)) or not all(
        isinstance(value, str) and value for value in evidence_value
    ):
        raise ValueError("asset.attest evidence_asset_ids must be strings")
    contracts = {contract.id: contract for contract in context.contracts}
    assets = {asset.id: asset for asset in context.assets}
    contract = contracts.get(contract_id)
    asset = assets.get(asset_id)
    if contract is None:
        raise ValueError(f"asset.attest references unknown Contract {contract_id!r}")
    if asset is None:
        raise ValueError(f"asset.attest references unknown Asset {asset_id!r}")
    if output_name not in contract.outputs or asset.name != output_name:
        raise ValueError("asset.attest must bind an exact declared output slot")
    if asset.created_by != contract.id:
        raise ValueError("asset.attest target was not produced for this Contract")
    if asset.signed_by == candidate.actor_id:
        raise ValueError("asset producer cannot attest its own output")
    policy = contract.acceptance_policy
    if policy is None or policy.get("mode") != "independent":
        raise ValueError("asset.attest requires independent Contract acceptance")
    expected_policy_version = policy.get("policy_version")
    if not isinstance(expected_policy_version, str) or not expected_policy_version:
        raise ValueError("independent acceptance requires a policy_version")
    expected_policy_id = acceptance_policy_id(policy)
    if supplied_policy_id != expected_policy_id:
        raise ValueError("asset.attest acceptance policy identity does not match")
    if supplied_policy_version != expected_policy_version:
        raise ValueError("asset.attest acceptance policy version does not match")
    rubric_ids = tuple(str(value) for value in rubric_value)
    evidence_ids = tuple(str(value) for value in evidence_value)
    expected_rubric_ids = tuple(policy.get("rubric_asset_ids", ()))
    expected_evidence_ids = tuple(policy.get("evidence_asset_ids", ()))
    if rubric_ids != expected_rubric_ids:
        raise ValueError("asset.attest rubric Asset IDs do not match policy")
    if evidence_ids != expected_evidence_ids:
        raise ValueError("asset.attest evidence Asset IDs do not match policy")
    missing_context = tuple(
        value for value in (*rubric_ids, *evidence_ids) if value not in assets
    )
    if missing_context:
        raise ValueError(
            f"asset.attest references unknown rubric/evidence Assets {missing_context!r}"
        )
    committed_record_ids = {
        str(record.payload.get("asset", {}).get("id", "")): record.id
        for record in context.runtime_records
        if record.record_type == "asset.committed"
        and isinstance(record.payload.get("asset"), Mapping)
    }
    asset_parent_id = committed_record_ids.get(asset_id)
    evidence_parent_ids = tuple(
        committed_record_ids[value]
        for value in (*rubric_ids, *evidence_ids)
        if value in committed_record_ids
    )
    if asset_parent_id is None:
        raise ValueError("asset.attest target has no immutable committed fact")
    missing_context_facts = tuple(
        value
        for value in (*rubric_ids, *evidence_ids)
        if value not in committed_record_ids
    )
    if missing_context_facts:
        raise ValueError(
            "asset.attest rubric/evidence has no immutable committed fact: "
            f"{missing_context_facts!r}"
        )
    prior_attestations = {
        str(record.payload.get("verifier_actor_id", ""))
        for record in context.runtime_records
        if record.record_type == "asset.attested"
        and record.payload.get("contract_id") == contract_id
        and record.payload.get("output_name") == output_name
        and record.payload.get("asset_id") == asset_id
        and record.payload.get("verdict") == "accepted"
    }
    if candidate.actor_id in prior_attestations:
        raise ValueError("verifier actor has already attested this output Asset")
    attestation = create_runtime_record(
        "asset.attested",
        {
            "asset_id": asset_id,
            "contract_id": contract_id,
            "evidence_asset_ids": list(evidence_ids),
            "output_name": output_name,
            "policy_id": expected_policy_id,
            "policy_version": expected_policy_version,
            "rubric_asset_ids": list(rubric_ids),
            "verdict": verdict,
            "verifier_actor_id": candidate.actor_id,
            "verifier_key_id": candidate.key_id,
        },
        causal_parents=(receipt_id, asset_parent_id, *evidence_parent_ids),
    )
    records: tuple[RuntimeRecord, ...] = (attestation,)
    already_qualified = any(
        record.record_type == "output.qualified"
        and record.payload.get("contract_id") == contract_id
        and record.payload.get("output_name") == output_name
        and record.payload.get("asset_id") == asset_id
        for record in context.runtime_records
    )
    accepted_verifiers = prior_attestations | (
        {candidate.actor_id} if verdict == "accepted" else set()
    )
    if not already_qualified and accepted_verifiers:
        qualification = create_runtime_record(
            "output.qualified",
            {
                "asset_id": asset_id,
                "contract_id": contract_id,
                "output_name": output_name,
                "policy_id": expected_policy_id,
                "verifier_actor_ids": sorted(accepted_verifiers),
            },
            causal_parents=(attestation.id,),
        )
        records = (*records, qualification)
    return AttestationProjection(
        records=records,
        relation_target=f"{contract_id}:{output_name}:{asset_id}",
        additional_capabilities=tuple(policy.get("verifier_capabilities", ())),
    )


def validate_output_qualification_commit(
    existing_records: Sequence[RuntimeRecord],
    pending_records: Sequence[RuntimeRecord],
) -> None:
    """Enforce one immutable selected Asset per Contract output slot."""
    selected: dict[tuple[str, str], str] = {}
    for record in (*tuple(existing_records), *tuple(pending_records)):
        if record.record_type != "output.qualified":
            continue
        slot = (
            str(record.payload.get("contract_id", "")),
            str(record.payload.get("output_name", "")),
        )
        asset_id = str(record.payload.get("asset_id", ""))
        prior = selected.setdefault(slot, asset_id)
        if prior != asset_id:
            raise OutputQualificationConflict(
                "qualified output slot already selects a different immutable Asset"
            )


def materialize_qualification_facts(
    store,
    pending_records: Sequence[RuntimeRecord],
) -> tuple[tuple[TraceEntry, ...], tuple[RuntimeRecord, ...]]:
    """Complete independently accepted Contracts in the attestation transaction."""
    qualifications = tuple(
        record for record in pending_records if record.record_type == "output.qualified"
    )
    if not qualifications:
        return (), ()
    pending_ids: dict[tuple[str, str], set[str]] = {}
    causal_ids: dict[str, list[str]] = {}
    for record in qualifications:
        contract_id = str(record.payload["contract_id"])
        output_name = str(record.payload["output_name"])
        asset_id = str(record.payload["asset_id"])
        pending_ids.setdefault((contract_id, output_name), set()).add(asset_id)
        causal_ids.setdefault(contract_id, []).append(record.id)
    terminal_contracts = {
        str(record.payload.get("contract_id", ""))
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    }
    traces: list[TraceEntry] = []
    records: list[RuntimeRecord] = []
    for contract_id in sorted(causal_ids):
        if contract_id in terminal_contracts:
            continue
        contract = store.get_contract(contract_id)
        if contract is None or not all_outputs_satisfied(
            contract,
            store,
            extra_qualified_asset_ids=pending_ids,
        ):
            continue
        entry = replace(
            create_entry(
                contract.id,
                "complete",
                authority_policy="independent",
                authority_result="qualified",
                accepted_fragments=[
                    f"[qualified] {output}: {sorted(asset_ids)}"
                    for (cid, output), asset_ids in sorted(pending_ids.items())
                    if cid == contract.id
                ],
                budget_remaining=0,
            ),
            timestamp="",
        )
        terminal = create_terminal_record(contract.id, "complete")
        trace_record = create_runtime_record(
            "trace.recorded",
            {"trace": trace_entry_to_dict(entry)},
            causal_parents=(terminal.id,),
        )
        traces.append(entry)
        records.extend((terminal, trace_record))
    return tuple(traces), tuple(records)
