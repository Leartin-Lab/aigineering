"""Independent output qualification and terminal consequence reduction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math

from aigineering.core.output_satisfaction import (
    output_asset_meets_conditions,
)
from aigineering.core.projection_store_view import ProjectionStoreView
from aigineering.core.publication_history import (
    read_effect_types,
    validate_published_member,
)
from aigineering.core.ids import acceptance_policy_id
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal


def validate_asset_publication(context, asset):
    """Prove one exact admitted Asset and its immutable publication membership."""
    from aigineering.core.asset_graph_facts import asset_from_graph_values
    from aigineering.protocol.runtime_record import validate_runtime_record
    from aigineering.protocol.wire import asset_to_dict

    rows = context.runtime_records
    committed_by_id = tuple(
        row
        for row in rows
        if row.record_type == "asset.committed"
        and isinstance(row.payload.get("asset"), Mapping)
        and row.payload["asset"].get("id") == asset.id
    )
    if len(committed_by_id) != 1:
        raise ValueError("task output has no unique immutable publication fact")
    committed = committed_by_id[0]
    validate_runtime_record(committed)
    if (
        committed.payload.get("asset") != asset_to_dict(asset)
        or committed.payload.get("contract_id") != asset.created_by
    ):
        raise ValueError("task output publication differs from its exact Asset")
    if asset.definition_hash.startswith("definition:v1:"):
        assertions = tuple(
            row
            for row in rows
            if row.record_type == "asset.definition-content.asserted"
            and isinstance(row.payload.get("assertion"), Mapping)
            and committed.causal_parents == (row.id,)
        )
        if len(assertions) != 1:
            raise ValueError("graph task output has no exact assertion publication")
        assertion = assertions[0]
        content_id = assertion.payload["assertion"].get("content_id")
        definition_id = assertion.payload["assertion"].get("definition_id")
        content_rows = tuple(
            row
            for row in rows
            if row.record_type == "asset.content.published"
            and isinstance(row.payload.get("content"), Mapping)
            and row.payload["content"].get("id") == content_id
        )
        definition_rows = tuple(
            row
            for row in rows
            if row.record_type == "asset.definition.published"
            and isinstance(row.payload.get("definition"), Mapping)
            and row.payload["definition"].get("id") == definition_id
        )
        for row in (*content_rows, *definition_rows):
            validate_runtime_record(row)
        contents = tuple(row.payload["content"] for row in content_rows)
        definitions = tuple(row.payload["definition"] for row in definition_rows)
        if (
            not contents
            or any(item != contents[0] for item in contents[1:])
            or not definitions
            or any(item != definitions[0] for item in definitions[1:])
            or asset_from_graph_values(
                contents[0], definitions[0], assertion.payload["assertion"]
            )
            != asset
        ):
            raise ValueError("graph task output differs from its exact assertion")
        member = assertion
    else:
        member = committed
    receipt, head = validate_published_member(rows, member)
    if committed.id not in head.payload.get("committed_record_ids", ()):
        raise ValueError("task output fact is outside its publication head")
    return receipt, head, member


def validate_claim_bound_output(context, asset, producer=None):
    """Prove an output belongs to its producer's exact submitted claim."""
    from aigineering.protocol.runtime_record import validate_runtime_record

    receipt, head, member = validate_asset_publication(context, asset)
    if producer is None:
        producer = next(
            (
                contract
                for contract in context.contracts
                if contract.id == receipt.payload.get("contract_id")
            ),
            None,
        )
    policy = producer.acceptance_policy if producer is not None else None
    requirements = policy.get("output_requirements", {}) if policy is not None else {}
    requirement = requirements.get(asset.name, {})
    shared_definition = requirement.get("definition_id") == asset.definition_hash
    if (
        producer is None
        or asset.name not in producer.outputs
        or (asset.created_by != producer.id and not shared_definition)
    ):
        raise ValueError("claim output does not match a producer-declared output")
    if receipt.payload.get("contract_id") != producer.id:
        raise ValueError("task output receipt is not bound to its producer Contract")
    if shared_definition and receipt.payload.get("actor_id") != asset.signed_by:
        raise ValueError("task output signer differs from its claim actor")
    if shared_definition:
        assertion = member.payload.get("assertion", {})
        evidence = assertion.get("evidence", {})
        if (
            assertion.get("actor_id") != receipt.payload.get("actor_id")
            or assertion.get("key_id") != receipt.payload.get("key_id")
            or not isinstance(evidence, Mapping)
            or evidence.get("contract_id") != producer.id
            or evidence.get("output_name") != asset.name
        ):
            raise ValueError("shared Definition output lacks its exact claim evidence")
    rows = context.runtime_records
    submitted_rows = tuple(
        row
        for row in rows
        if row.record_type == "claim.submitted"
        and row.payload.get("candidate_id") == receipt.payload.get("candidate_id")
    )
    if len(submitted_rows) != 1:
        raise ValueError("task output Candidate has no unique claim submission")
    submitted = submitted_rows[0]
    validate_runtime_record(submitted)
    claim_id = submitted.payload.get("claim_id")
    if (
        not isinstance(claim_id, str)
        or not claim_id
        or not isinstance(submitted.payload.get("package_id"), str)
        or not submitted.payload.get("package_id")
        or type(submitted.payload.get("epoch")) is not int
        or submitted.payload.get("epoch") < 1
    ):
        raise ValueError("task output claim submission has malformed typed bindings")
    grants = tuple(
        row
        for row in rows
        if row.record_type == "claim.granted"
        and row.id in submitted.causal_parents
        and row.payload.get("claim_id") == claim_id
    )
    if len(grants) != 1:
        raise ValueError("task output submission has no exact claim grant")
    grant = grants[0]
    validate_runtime_record(grant)
    if (
        submitted.causal_parents
        != (
            grant.id,
            receipt.id if head.payload.get("closure_version") == 1 else head.id,
        )
        or (
            head.payload.get("closure_version") == 1
            and submitted.id not in head.payload["committed_record_ids"]
        )
        or submitted.payload.get("contract_id") != producer.id
        or submitted.payload.get("worker_id") != receipt.payload.get("actor_id")
        or receipt.payload.get("claim_id") != claim_id
        or grant.payload.get("contract_id") != producer.id
        or grant.payload.get("worker_id") != submitted.payload.get("worker_id")
        or grant.payload.get("package_id") != submitted.payload.get("package_id")
        or grant.payload.get("epoch") != submitted.payload.get("epoch")
        or type(grant.payload.get("epoch")) is not int
        or grant.payload.get("epoch") < 1
    ):
        raise ValueError("task output claim bindings do not match the exact grant")
    if grant.causal_parents:
        if len(grant.causal_parents) != 1:
            raise ValueError("task output claim grant has malformed request provenance")
        request_id = grant.causal_parents[0]
        requests = tuple(
            row
            for row in rows
            if row.id == request_id and row.record_type == "worker.claim.requested"
        )
        if len(requests) != 1 or requests[0].payload.get(
            "worker_id"
        ) != grant.payload.get("worker_id"):
            raise ValueError(
                "task output claim grant request does not match its Worker"
            )
        request = requests[0]
        validate_runtime_record(request)
        if request.payload.get("contract_id") != grant.payload.get("contract_id"):
            raise ValueError(
                "task output claim request does not match its granted Contract"
            )
        request_receipts = tuple(
            row
            for row in rows
            if row.record_type == "candidate.received"
            and row.id in request.causal_parents
            and row.payload.get("candidate_id") == request.payload.get("candidate_id")
            and row.payload.get("actor_id") == grant.payload.get("worker_id")
        )
        if len(request.causal_parents) != 1 or len(request_receipts) != 1:
            raise ValueError("task output claim request lacks its signed receipt")
        validate_runtime_record(request_receipts[0])
        if "worker.claim" not in read_effect_types(request_receipts[0].payload):
            raise ValueError("task output claim request receipt has the wrong effect")
    return producer


@dataclass(frozen=True)
class AttestationProjection:
    records: tuple[RuntimeRecord, ...]
    relation_target: str
    additional_capabilities: tuple[str, ...]


class OutputQualificationConflict(ValueError):
    """An output qualification conflicts with canonical policy or selection."""


def _qualification_record(
    attestation: RuntimeRecord, accepted_verifiers: set[str]
) -> RuntimeRecord:
    payload = attestation.payload
    return create_runtime_record(
        "output.qualified",
        {
            "asset_id": payload["asset_id"],
            "contract_id": payload["contract_id"],
            "output_name": payload["output_name"],
            "policy_id": payload["policy_id"],
            "verifier_actor_ids": sorted(accepted_verifiers),
        },
        causal_parents=(attestation.id,),
    )


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
    view = ProjectionStoreView(context)
    assets = {asset.id: asset for asset in context.assets}
    contract = view.get_contract(contract_id)
    asset = assets.get(asset_id)
    if contract is None:
        raise ValueError(f"asset.attest references unknown Contract {contract_id!r}")
    if asset is None:
        raise ValueError(f"asset.attest references unknown Asset {asset_id!r}")
    if output_name not in contract.outputs or asset.name != output_name:
        raise ValueError("asset.attest must bind an exact declared output slot")
    policy = contract.acceptance_policy
    if policy is None or policy.get("mode") != "independent":
        raise ValueError("asset.attest requires independent Contract acceptance")
    validate_output_shape(policy, output_name, asset.content)
    if not output_asset_meets_conditions(contract, output_name, asset):
        raise ValueError("asset.attest target does not meet declared Asset conditions")
    validate_asset_publication(context, asset)
    if asset.signed_by == candidate.actor_id:
        raise ValueError("asset producer cannot attest its own output")
    expected_policy_version = policy.get("policy_version")
    if not isinstance(expected_policy_version, str) or not expected_policy_version:
        raise ValueError("independent acceptance requires a policy_version")
    expected_policy_id = acceptance_policy_id(policy)
    if supplied_policy_id and supplied_policy_id != expected_policy_id:
        raise ValueError("asset.attest acceptance policy identity does not match")
    if supplied_policy_version and supplied_policy_version != expected_policy_version:
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
        qualification = _qualification_record(attestation, accepted_verifiers)
        records = (*records, qualification)
    return AttestationProjection(
        records=records,
        relation_target=f"{contract_id}:{output_name}:{asset_id}",
        additional_capabilities=tuple(policy.get("verifier_capabilities", ())),
    )


def _output_shape_error(value: object, shape: object, path: str = "$") -> str | None:
    """Validate the deliberately small deterministic JSON shape language."""
    if shape == "string":
        return None if isinstance(value, str) else f"{path} must be a string"
    if shape == "nonempty_string":
        return (
            None
            if isinstance(value, str) and bool(value.strip())
            else f"{path} must be a non-empty string"
        )
    if shape == "number":
        is_finite_number = (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and math.isfinite(value)
        )
        return None if is_finite_number else f"{path} must be a finite number"
    if shape == "boolean":
        return None if isinstance(value, bool) else f"{path} must be a boolean"
    if isinstance(shape, Mapping):
        if not isinstance(value, Mapping):
            return f"{path} must be an object"
        if set(value) != set(shape):
            expected = sorted(str(key) for key in shape)
            return f"{path} keys must be exactly {expected}"
        for key in sorted(shape, key=str):
            error = _output_shape_error(value[key], shape[key], f"{path}.{key}")
            if error is not None:
                return error
        return None
    if isinstance(shape, (list, tuple)) and len(shape) == 1:
        if not isinstance(value, list) or not value:
            return f"{path} must be a non-empty array"
        for index, item in enumerate(value):
            error = _output_shape_error(item, shape[0], f"{path}[{index}]")
            if error is not None:
                return error
        return None
    return f"{path} uses an unsupported output shape"


def _reject_non_finite_json_constant(value: str):
    raise ValueError(f"non-finite JSON constant {value}")


def validate_output_shape(
    policy: Mapping[str, object], output_name: str, content: str
) -> None:
    """Reject content that violates an immutable deterministic output shape."""
    output_shapes = policy.get("output_shapes", {})
    if not isinstance(output_shapes, Mapping):
        raise ValueError("acceptance output_shapes must be an object")
    if output_name not in output_shapes:
        return
    try:
        output_value = json.loads(
            content, parse_constant=_reject_non_finite_json_constant
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("acceptance output shape requires valid JSON") from exc
    shape_error = _output_shape_error(output_value, output_shapes[output_name])
    if shape_error is not None:
        raise ValueError(f"acceptance output shape mismatch: {shape_error}")


def validate_output_qualification_batch(
    existing_records: Sequence[RuntimeRecord],
    pending_records: Sequence[RuntimeRecord],
) -> None:
    """Project at most one selected Asset per slot across the whole core batch."""
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
