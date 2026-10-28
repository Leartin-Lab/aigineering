"""Pure Candidate-native active task handoff projection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
import unicodedata

from aigineering.core.causal_allowance import (
    allowance_available,
    project_causal_allowance_returns,
)
from aigineering.core.claim_holds import open_claim_holds
from aigineering.core.claim_assignments import (
    ASSIGNMENT_FACT,
    AssignmentAdmission,
    AssignmentIntent,
    AssignmentMode,
    AssignmentTransition,
    SuccessorAssignmentWitness,
    assignment_context_from_projection,
    assignment_head,
    build_assignment_fact,
)
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.claim_constraints import external_prerequisite_blockers
from aigineering.core.effect_projectors import (
    EffectProjection,
    project_contract_cancellation,
    project_contract_declaration,
)
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.effect_builders import (
    contract_cancellation_effect,
    contract_declaration_effect,
)
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    create_runtime_record,
    validate_runtime_record,
)
from aigineering.protocol.types import Contract
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.wire import contract_from_dict
from aigineering.plugins.task_successors import (
    build_successor_contract,
    freeze_successor_disclosure,
    remaining_output_slots,
    satisfied_output_slots,
)
from aigineering.plugins.handoff_tree import inventory_handoff_tree
from aigineering.plugins.successor_publications import (
    validate_successor_publications,
)


@dataclass(frozen=True)
class TaskEditIntent:
    """Validated exact text and snapshot references for an edit handoff."""

    title: str | None
    body: str | None
    source_assignment_id: str
    source_hold_ids: tuple[str, ...]

    @property
    def changed_fields(self) -> tuple[str, ...]:
        return tuple(
            field for field in ("title", "body") if getattr(self, field) is not None
        )


@dataclass(frozen=True)
class HandoffPublicationWitness:
    """Exact read-only witnesses for one accepted handoff relation."""

    source_contract_id: str
    target_contract_id: str
    source_declaration_id: str
    source_receipt_id: str
    source_head_id: str
    handoff_link_id: str
    handoff_receipt_id: str
    handoff_head_id: str
    cancelled_terminal_id: str
    target_declaration_id: str
    target_receipt_id: str
    target_head_id: str


@dataclass(frozen=True)
class HandoffPublicationIssue:
    """Safe known endpoints and fact reference for an unverified handoff."""

    code: str
    source_contract_id: str | None = None
    target_contract_id: str | None = None
    record_id: str | None = None


def validate_task_handoff_relations(
    query_context, records: tuple, publication_index
) -> tuple[tuple[HandoffPublicationWitness, ...], tuple[HandoffPublicationIssue, ...]]:
    """Verify accepted handoff links using one immutable query and shared proof index.

    This proves only the published source-to-successor relation. Variant-specific
    budget, tree, hold, assignment, and allowance decisions remain owned by the
    handoff publisher and are not re-evaluated against current state here.
    """
    witnesses, issues = validate_successor_publications(
        query_context,
        records,
        publication_index,
        link_record_type="task.handed_off",
        sole_effect_type="task.handoff",
        source_contract_field="contract_id",
        target_contract_field="successor_id",
    )
    return (
        tuple(
            HandoffPublicationWitness(
                source_contract_id=item.source_contract_id,
                target_contract_id=item.target_contract_id,
                source_declaration_id=item.source_declaration_id,
                source_receipt_id=item.source_receipt_id,
                source_head_id=item.source_head_id,
                handoff_link_id=item.link_record_id,
                handoff_receipt_id=item.link_receipt_id,
                handoff_head_id=item.link_head_id,
                cancelled_terminal_id=item.cancelled_terminal_id,
                target_declaration_id=item.target_declaration_id,
                target_receipt_id=item.target_receipt_id,
                target_head_id=item.target_head_id,
            )
            for item in witnesses
        ),
        tuple(
            HandoffPublicationIssue(
                code="handoff_publication_unverified",
                source_contract_id=item.source_contract_id,
                target_contract_id=item.target_contract_id,
                record_id=item.record_id,
            )
            for item in issues
        ),
    )


def validate_task_edit_request(edit: object, source: Contract) -> TaskEditIntent:
    """Validate edit shape and text without comparing mutable source state.

    This is safe for both a fresh request and exact replay after the source's
    open holds have later changed. Fresh callers separately use
    ``validate_task_edit_snapshot`` before provisioning.
    """
    if not isinstance(edit, Mapping):
        raise ValueError("task.handoff edit must be an object")
    required = {"source_assignment_id", "source_hold_ids"}
    allowed = required | {"title", "body"}
    if not required.issubset(edit) or not set(edit).issubset(allowed):
        raise ValueError("task.handoff edit fields are invalid")
    if "title" not in edit and "body" not in edit:
        raise ValueError("task.handoff edit must replace title, body, or both")

    title = edit.get("title")
    if "title" in edit:
        title = _required_text(title, "edit.title")
    else:
        title = None
    body = edit.get("body")
    if "body" in edit:
        body = _edit_body(body)
    else:
        body = None
    effective_title = source.name if title is None else title
    effective_body = source.description if body is None else body
    if unicodedata.normalize("NFC", effective_title) == unicodedata.normalize(
        "NFC", source.name
    ) and unicodedata.normalize("NFC", effective_body) == unicodedata.normalize(
        "NFC", source.description
    ):
        raise ValueError("task.handoff edit is a canonical no-op")

    assignment_id = edit["source_assignment_id"]
    if not isinstance(assignment_id, str) or (
        assignment_id and not assignment_id.strip()
    ):
        raise ValueError("task.handoff edit source_assignment_id must be text")
    raw_holds = edit["source_hold_ids"]
    if isinstance(raw_holds, (str, bytes)) or not isinstance(raw_holds, Sequence):
        raise ValueError("task.handoff edit source_hold_ids must be an array")
    if len(raw_holds) > 64:
        raise ValueError("task.handoff edit supports at most 64 source holds")
    if any(not isinstance(item, str) or not item.strip() for item in raw_holds):
        raise ValueError("task.handoff edit hold IDs must be non-empty text")
    hold_ids = tuple(raw_holds)
    if hold_ids != tuple(sorted(set(hold_ids))):
        raise ValueError("task.handoff edit hold IDs must be sorted and unique")
    return TaskEditIntent(title, body, assignment_id, hold_ids)


def validate_task_edit_snapshot(
    intent: TaskEditIntent,
    *,
    source_assignment_id: str,
    source_hold_ids: Sequence[str],
) -> None:
    """Require an edit's signed assignment/hold snapshot to match fresh state."""
    if intent.source_assignment_id != source_assignment_id:
        raise ValueError("task.handoff edit source assignment head changed")
    if intent.source_hold_ids != tuple(source_hold_ids):
        raise ValueError("task.handoff edit source hold set changed")


def _edit_body(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("task.handoff edit.body must be text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("task.handoff edit.body is not valid UTF-8") from exc
    if len(encoded) > 65536:
        raise ValueError("task.handoff edit.body exceeds 65536 UTF-8 bytes")
    return value


def task_edit_snapshot(
    source: Contract, context: EffectProjectionContext
) -> tuple[str, tuple[str, ...]]:
    """Return the canonical source assignment head and sorted open hold IDs."""
    assignment = assignment_head(
        assignment_context_from_projection(context), contract_id=source.id
    )
    assignment_id = assignment.id if assignment is not None else ""
    hold_ids = tuple(
        sorted(
            record.id
            for record in open_claim_holds(
                context.runtime_records, contract_id=source.id
            )
        )
    )
    return assignment_id, hold_ids


def _replay_projection(
    candidate: CandidateProposal,
    context: EffectProjectionContext,
) -> EffectProjection | None:
    matching_handoffs = [
        record
        for record in context.runtime_records
        if record.record_type == "task.handed_off"
        and record.payload.get("candidate_id") == candidate.id
    ]
    if not matching_handoffs:
        return None
    if len(matching_handoffs) != 1:
        raise ValueError("handoff replay has duplicate Candidate links")
    handoff = matching_handoffs[0]
    committed_matches = [
        record
        for record in context.runtime_records
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == candidate.id
    ]
    if len(committed_matches) != 1:
        raise ValueError("handoff replay has no unique committed Candidate head")
    committed = committed_matches[0]
    by_id = {record.id: record for record in context.runtime_records}
    record_ids = committed.payload.get("committed_record_ids")
    if (
        not isinstance(record_ids, (list, tuple))
        or len(record_ids) != len(set(record_ids))
        or any(record_id not in by_id for record_id in record_ids)
    ):
        raise ValueError("handoff replay is missing committed projection facts")
    successor = next(
        (
            contract
            for contract in context.contracts
            if contract.id == handoff.payload.get("successor_id")
        ),
        None,
    )
    if successor is None:
        raise ValueError("handoff replay successor Contract is missing")
    records = tuple(by_id[record_id] for record_id in record_ids)
    intents = ()
    witnesses = ()
    edit_metadata = handoff.payload.get("edit")
    if edit_metadata is not None:
        source_id = handoff.payload.get("contract_id")
        sources = [item for item in context.contracts if item.id == source_id]
        if len(sources) != 1 or not isinstance(edit_metadata, Mapping):
            raise ValueError("task edit replay source or metadata is invalid")
        source = sources[0]
        source_declaration_id = edit_metadata.get("source_declaration_id")
        successor_declaration_id = edit_metadata.get("successor_declaration_id")
        source_declaration = by_id.get(source_declaration_id)
        successor_declaration = by_id.get(successor_declaration_id)
        if (
            source_declaration is None
            or successor_declaration is None
            or source_declaration.record_type != "contract.declared"
            or successor_declaration.record_type != "contract.declared"
            or successor_declaration not in records
        ):
            raise ValueError("task edit replay declaration evidence is missing")
        _validate_declaration_matches_contract(source_declaration, source)
        _validate_declaration_matches_contract(successor_declaration, successor)
        edit_effects = [
            effect
            for effect in candidate.effects
            if effect.effect_type == "task.handoff"
        ]
        if len(edit_effects) != 1 or "edit" not in edit_effects[0].payload:
            raise ValueError("task edit replay Candidate has no nested edit intent")
        intent = validate_task_edit_request(edit_effects[0].payload["edit"], source)
        expected_candidate_parents = (
            source.id,
            source_declaration.id,
            *((intent.source_assignment_id,) if intent.source_assignment_id else ()),
            *intent.source_hold_ids,
        )
        if candidate.causal_parents != expected_candidate_parents:
            raise ValueError("task edit replay Candidate parents are invalid")
        link_intent = {
            "changed_fields": intent.changed_fields,
            "source_declaration_id": source_declaration.id,
            "successor_declaration_id": successor_declaration.id,
            "source_assignment_id": intent.source_assignment_id,
            "source_hold_ids": intent.source_hold_ids,
        }
        for name, expected in link_intent.items():
            actual = edit_metadata.get(name)
            if name in {"changed_fields", "source_hold_ids"}:
                actual = tuple(actual) if isinstance(actual, (tuple, list)) else actual
            if actual != expected:
                raise ValueError(f"task edit replay {name} reference is invalid")
        if (intent.title is None and successor.name != source.name) or (
            intent.title is not None and successor.name != intent.title
        ):
            raise ValueError("task edit replay title does not match successor")
        if (intent.body is None and successor.description != source.description) or (
            intent.body is not None and successor.description != intent.body
        ):
            raise ValueError("task edit replay body does not match successor")
        raw_mapping = edit_metadata.get("hold_mapping")
        if set(edit_metadata) != {
            "changed_fields",
            "source_declaration_id",
            "successor_declaration_id",
            "source_assignment_id",
            "source_hold_ids",
            "hold_mapping",
        }:
            raise ValueError("task edit replay metadata fields are invalid")
        receipts = [
            record
            for record in context.runtime_records
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id") == candidate.id
        ]
        if len(receipts) != 1:
            raise ValueError("task edit replay receipt is missing or ambiguous")
        receipt = receipts[0]
        receipt_id = receipt.id
        if not isinstance(raw_mapping, (tuple, list)) or len(raw_mapping) != len(
            intent.source_hold_ids
        ):
            raise ValueError("task edit replay hold mapping is incomplete")
        copied_ids: list[str] = []
        for source_hold_id, entry in zip(
            intent.source_hold_ids, raw_mapping, strict=True
        ):
            source_hold = by_id.get(source_hold_id)
            if not isinstance(entry, Mapping) or set(entry) != {
                "source_hold_id",
                "successor_hold_id",
            }:
                raise ValueError("task edit replay hold mapping entry is malformed")
            copied_id = entry["successor_hold_id"]
            copied = by_id.get(copied_id) if isinstance(copied_id, str) else None
            if (
                entry["source_hold_id"] != source_hold_id
                or source_hold is None
                or source_hold.record_type != "claim.hold.placed"
                or source_hold.payload.get("contract_id") != source.id
                or copied is None
                or copied.record_type != "claim.hold.placed"
                or copied not in records
                or copied.payload.get("contract_id") != successor.id
                or copied.payload.get("reason") != source_hold.payload.get("reason")
                or copied.payload.get("candidate_id") != candidate.id
                or copied.payload.get("actor_id") != candidate.actor_id
                or copied.payload.get("key_id") != candidate.key_id
                or receipt_id not in copied.causal_parents
                or source_hold_id not in copied.causal_parents
                or successor_declaration.id not in copied.causal_parents
                or copied.payload.get("candidate_id") != candidate.id
            ):
                raise ValueError("task edit replay copied hold evidence is invalid")
            copied_ids.append(copied.id)
        if intent.source_assignment_id:
            assignment = by_id.get(intent.source_assignment_id)
            if (
                assignment is None
                or assignment.record_type != ASSIGNMENT_FACT
                or assignment.payload.get("contract_id") != source.id
            ):
                raise ValueError("task edit replay source assignment is invalid")
        if len(copied_ids) != len(set(copied_ids)):
            raise ValueError("task edit replay duplicates copied hold facts")
        positions = {record.id: position for position, record in enumerate(records)}
        if any(
            positions[copied_id] >= positions[handoff.id]
            or positions[successor_declaration.id] >= positions[copied_id]
            for copied_id in copied_ids
        ):
            raise ValueError("task edit replay hold placement order is invalid")
        mapping = tuple(
            {
                "source_hold_id": source_hold_id,
                "successor_hold_id": copied_id,
            }
            for source_hold_id, copied_id in zip(
                intent.source_hold_ids, copied_ids, strict=True
            )
        )
        if tuple(raw_mapping) != mapping:
            raise ValueError("task edit replay hold mapping is not canonical")
        if (
            receipt.payload.get("actor_id") != candidate.actor_id
            or receipt.payload.get("key_id") != candidate.key_id
            or receipt.payload.get("domain_id") != candidate.domain_id
            or tuple(receipt.payload.get("effect_types", ())) != ("task.handoff",)
            or receipt.payload.get("signature") != candidate.signature
            or receipt.payload.get("signature_kind") != candidate.signature_kind
            or tuple(receipt.causal_parents) != candidate.causal_parents
        ):
            raise ValueError("task edit replay receipt does not match Candidate")
        receipt_positions = [
            index
            for index, record in enumerate(context.runtime_records)
            if record.id == receipt.id
        ]
        if len(receipt_positions) != 1:
            raise ValueError("task edit replay receipt is ambiguous in the fact order")
        prior_records = context.runtime_records[: receipt_positions[0]]
        if source_declaration not in prior_records:
            raise ValueError(
                "task edit source declaration was not prior to its receipt"
            )
        historical_holds = tuple(
            sorted(
                record.id
                for record in open_claim_holds(prior_records, contract_id=source.id)
            )
        )
        if historical_holds != intent.source_hold_ids:
            raise ValueError("task edit replay source hold snapshot is invalid")
        prior_context = replace(context, runtime_records=prior_records)
        prior_assignment = assignment_head(
            assignment_context_from_projection(prior_context), contract_id=source.id
        )
        historical_assignment_id = prior_assignment.id if prior_assignment else ""
        if historical_assignment_id != intent.source_assignment_id:
            raise ValueError("task edit replay source assignment snapshot is invalid")
        terminal = next(
            (
                record
                for record in records
                if record.record_type == "lifecycle.terminal"
                and record.payload.get("contract_id") == source.id
            ),
            None,
        )
        if terminal is None or handoff not in records:
            raise ValueError("task edit replay terminal/link membership is invalid")
        required_parents = {
            receipt_id,
            terminal.id,
            successor_declaration.id,
            source_declaration.id,
            *intent.source_hold_ids,
            *copied_ids,
            *((intent.source_assignment_id,) if intent.source_assignment_id else ()),
            *(
                (str(handoff.payload.get("assignment_fact_id")),)
                if handoff.payload.get("assignment_fact_id")
                else ()
            ),
        }
        if not required_parents.issubset(handoff.causal_parents) or len(
            handoff.causal_parents
        ) != len(set(handoff.causal_parents)):
            raise ValueError("task edit replay causal parents are invalid")
        if committed.causal_parents != (receipt.id, *record_ids):
            raise ValueError("task edit replay committed head membership is invalid")
    assignment_fact_id = str(handoff.payload.get("assignment_fact_id", ""))
    assignment_mode_value = handoff.payload.get("assignment_mode")
    if assignment_mode_value is not None:
        try:
            mode = AssignmentMode(str(assignment_mode_value))
        except ValueError as exc:
            raise ValueError("handoff replay assignment mode is invalid") from exc
        declaration = next(
            (
                record
                for record in records
                if record.record_type == "contract.declared"
                and record.payload.get("contract", {}).get("id") == successor.id
            ),
            None,
        )
        source_terminal = next(
            (
                record
                for record in records
                if record.record_type == "lifecycle.terminal"
                and record.payload.get("contract_id")
                == handoff.payload.get("contract_id")
            ),
            None,
        )
        if declaration is None or source_terminal is None:
            raise ValueError("handoff replay assignment witness facts are missing")
        if assignment_fact_id:
            assignment_fact = by_id.get(assignment_fact_id)
            if (
                assignment_fact is None
                or assignment_fact.record_type != ASSIGNMENT_FACT
            ):
                raise ValueError("handoff replay assignment fact is missing")
            intents = (
                AssignmentIntent(
                    assignment_fact.id, successor.id, declaration.id, mode
                ),
            )
        witnesses = (
            SuccessorAssignmentWitness(
                source_contract_id=str(handoff.payload["contract_id"]),
                source_assignment_head_id=str(
                    handoff.payload.get("source_assignment_id", "")
                ),
                source_terminal_record_id=source_terminal.id,
                successor_contract_id=successor.id,
                successor_declaration_record_id=declaration.id,
                successor_assignment_record_id=assignment_fact_id,
                successor_link_record_id=handoff.id,
                assignment_mode=mode,
            ),
        )
    return EffectProjection(
        records=records,
        relation_target=str(handoff.payload["contract_id"]),
        contract=successor,
        additional_capabilities=(
            "contract.cancel",
            "contract.publish",
            *(("task.edit",) if edit_metadata is not None else ()),
            *(
                ("task.block",)
                if edit_metadata is not None and edit_metadata.get("source_hold_ids")
                else ()
            ),
            *(("task.block",) if handoff.payload.get("hold_id") else ()),
            *(
                ("task.assignment.manage",)
                if assignment_mode_value == AssignmentMode.OVERRIDE.value
                else ()
            ),
        ),
        assignment_intents=intents,
        successor_assignment_witnesses=witnesses,
    )


def project_task_handoff_effect(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    """Cancel one current attempt and atomically declare its bounded successor."""
    if effect.effect_type != "task.handoff":
        raise ValueError("handoff projector requires task.handoff")
    if candidate.effects != (effect,):
        raise ValueError("task.handoff Candidate must contain exactly one effect")
    payload = effect.payload
    expected_fields = {"contract_id", "reason", "budget", "hold_reason"}
    edit_intent = None
    if "edit" in payload:
        expected_fields.add("edit")
    if "assignment_override" in payload:
        expected_fields.add("assignment_override")
    if set(payload) != expected_fields:
        raise ValueError("task.handoff payload fields are invalid")
    contract_id = _required_text(payload["contract_id"], "contract_id")
    reason = _required_text(payload["reason"], "reason")
    hold_reason = payload["hold_reason"]
    if not isinstance(hold_reason, str):
        raise ValueError("task.handoff hold_reason must be text")
    if hold_reason:
        _required_text(hold_reason, "hold_reason")
    assignment_override = payload.get("assignment_override")
    if assignment_override is not None:
        if not isinstance(assignment_override, Mapping) or set(assignment_override) != {
            "source_assignment_id",
            "worker_id",
        }:
            raise ValueError("task.handoff assignment override fields are invalid")
        if not isinstance(
            assignment_override["source_assignment_id"], str
        ) or not isinstance(assignment_override["worker_id"], str):
            raise ValueError("task.handoff assignment override values must be text")
        if assignment_override["worker_id"]:
            _required_text(assignment_override["worker_id"], "assignment Worker ID")
    if "edit" in payload and (hold_reason or assignment_override is not None):
        raise ValueError(
            "task.handoff edit cannot combine hold_reason or assignment override"
        )
    requested_budget = payload["budget"]
    if requested_budget is not None and (
        type(requested_budget) is not int or requested_budget <= 0
    ):
        raise ValueError("task.handoff budget must be null or a positive integer")

    replay = _replay_projection(candidate, context)
    if replay is not None:
        return replay

    source = next(
        (contract for contract in context.contracts if contract.id == contract_id),
        None,
    )
    if source is None:
        raise ValueError(f"task.handoff references unknown Contract {contract_id!r}")
    edit_intent = (
        validate_task_edit_request(payload["edit"], source)
        if "edit" in payload
        else None
    )
    source_declaration = (
        _canonical_declaration(context, source.id) if edit_intent is not None else None
    )
    source_hold_records: tuple[RuntimeRecord, ...] = ()
    if edit_intent is not None:
        snapshot_assignment_id, snapshot_hold_ids = task_edit_snapshot(source, context)
        validate_task_edit_snapshot(
            edit_intent,
            source_assignment_id=snapshot_assignment_id,
            source_hold_ids=snapshot_hold_ids,
        )
        expected_parents = (
            source.id,
            source_declaration.id,
            *((snapshot_assignment_id,) if snapshot_assignment_id else ()),
            *snapshot_hold_ids,
        )
        if candidate.causal_parents != expected_parents:
            raise ValueError(
                "task.handoff edit Candidate parents do not match snapshot"
            )
        source_hold_records = tuple(
            sorted(
                open_claim_holds(context.runtime_records, contract_id=source.id),
                key=lambda record: record.id,
            )
        )
    if source.origin in {"system", "recovery"} or any(
        label.startswith(("plugin:", "method:")) for label in source.labels
    ):
        raise ValueError("task.handoff does not support plugin-controlled Contracts")
    if source.minting_authority:
        raise ValueError("task.handoff does not support protected minting Contracts")
    if any(
        record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == contract_id
        for record in context.runtime_records
    ):
        raise ValueError("task.handoff source Contract is already terminal")
    inventory = inventory_handoff_tree(
        source, context, allow_source_holds=edit_intent is not None
    )
    transfer_targets = frozenset(
        (source.id, *(contract.id for contract in inventory.cancelled))
    )
    dependency_blockers = external_prerequisite_blockers(
        context.runtime_records,
        cancelled_contract_ids=transfer_targets,
        repair_complete_contract_ids=frozenset(
            contract.id for contract in inventory.repairs
        ),
        repair_only_target_ids=frozenset(contract.id for contract in inventory.repairs),
    )
    if dependency_blockers:
        details = "; ".join(
            f"constraint {item['constraint_id']} targets Contract "
            f"{item['contract_id']} and requires Contract "
            f"{item['prerequisite_contract_id']} (observed "
            f"{item['observed_terminal']})"
            for item in dependency_blockers
        )
        raise ValueError("task.handoff blocked by external prerequisites: " + details)
    frozen_successor_disclosure = None
    if inventory.expanded:
        frozen_successor_disclosure = freeze_successor_disclosure(source, context)
        input_bindings = frozen_successor_disclosure[2].get("input_asset_bindings", {})
        missing_roles = tuple(
            role for role in source.inputs if not input_bindings.get(role)
        )
        if missing_roles:
            raise ValueError(
                "task.handoff cannot freeze empty source input roles: "
                + ", ".join(missing_roles)
                + "; provide an eligible input Asset before handing off"
            )

    completed_outputs = satisfied_output_slots(source, context)
    satisfied_names = {name for name, _ in completed_outputs}
    remaining_outputs = remaining_output_slots(source, context)
    if not remaining_outputs:
        raise ValueError("task.handoff source has no unsatisfied output slots")

    source_balance = allowance_available(source, context.runtime_records)
    cancel_effect = contract_cancellation_effect(source.id, reason)
    terminal = project_contract_cancellation(
        cancel_effect, candidate, receipt_id, context
    ).records[0]

    descendant_terminals: list[RuntimeRecord] = []
    for child in inventory.cancelled:
        descendant_terminals.append(
            project_contract_cancellation(
                contract_cancellation_effect(child.id, reason),
                candidate,
                receipt_id,
                context,
            ).records[0]
        )
    repair_terminals: list[RuntimeRecord] = []
    repair_proofs = dict(inventory.repair_outputs)
    for child in inventory.repairs:
        outputs = repair_proofs.get(child.id, ())
        repair_terminals.append(
            create_terminal_record(
                child.id,
                "complete",
                actor_id=candidate.actor_id,
                reason="handoff repaired historical output completion",
                causal_parents=(
                    receipt_id,
                    *(
                        asset_id
                        for _name, asset_ids, _proof_ids in outputs
                        for asset_id in asset_ids
                    ),
                    *(
                        proof_id
                        for _name, _asset_ids, proof_ids in outputs
                        for proof_id in proof_ids
                    ),
                ),
            )
        )

    pending_terminals = (terminal, *descendant_terminals, *repair_terminals)
    return_records: tuple[RuntimeRecord, ...] = ()
    if inventory.expanded and inventory.cancelled:
        return_records = project_causal_allowance_returns(
            source,
            inventory.cancelled,
            context.contracts,
            context.runtime_records,
            pending_terminals,
            candidate_id=candidate.id,
            receipt_id=receipt_id,
        )
    return_total = sum(int(record.payload["amount"]) for record in return_records)
    available = source_balance + return_total
    budget = available if requested_budget is None else requested_budget
    if budget <= 0:
        raise ValueError("task.handoff has no remaining causal allowance")
    if budget > available:
        raise ValueError("task.handoff budget exceeds current causal allowance")

    if inventory.expanded:
        if frozen_successor_disclosure is None:
            raise AssertionError("expanded successor disclosure was not frozen")
        frozen_context, _disclosure_ids, sensitive_policy = frozen_successor_disclosure
        successor = build_successor_contract(
            source,
            remaining_outputs,
            budget,
            reason,
            name_suffix=(
                ""
                if edit_intent is not None
                else f".handoff.expanded.{candidate.id.rsplit(':', 1)[-1]}"
            ),
            frozen_context_asset_ids=frozen_context,
            sensitive_input_policy=sensitive_policy,
            use_source_sensitive_policy=False,
            name_override=(
                source.name
                if edit_intent is not None and edit_intent.title is None
                else edit_intent.title
                if edit_intent is not None
                else None
            ),
            description_override=(
                source.description
                if edit_intent is not None and edit_intent.body is None
                else edit_intent.body
                if edit_intent is not None
                else None
            ),
        )
    else:
        if edit_intent is None:
            # Preserve the historical leaf successor identity byte-for-byte.
            successor = _successor_contract(source, remaining_outputs, budget, reason)
        else:
            successor = build_successor_contract(
                source,
                remaining_outputs,
                budget,
                reason,
                name_suffix="",
                name_override=(
                    source.name if edit_intent.title is None else edit_intent.title
                ),
                description_override=(
                    source.description if edit_intent.body is None else edit_intent.body
                ),
            )
    declare_effect = contract_declaration_effect(successor)
    declaration = project_contract_declaration(
        declare_effect, candidate, receipt_id, context
    )
    if declaration.contract is None:
        raise ValueError("handoff successor declaration failed")
    assignment_context = assignment_context_from_projection(context)
    source_assignment = assignment_head(assignment_context, contract_id=source.id)
    source_assignment_id = source_assignment.id if source_assignment else ""
    assignment_intents = ()
    successor_witnesses = ()
    successor_assignment_record: RuntimeRecord | None = None
    assignment_worker_id = ""
    assignment_mode: AssignmentMode | None = None
    if assignment_override is None:
        assignment_mode = AssignmentMode.INHERIT
        if source_assignment is not None:
            assignment_worker_id = str(source_assignment.payload.get("worker_id", ""))
            if assignment_worker_id:
                transition = AssignmentTransition(
                    target_contract_id=successor.id,
                    worker_id=assignment_worker_id,
                    previous_assignment_id="",
                    reason=reason,
                    mode=AssignmentMode.INHERIT,
                    source_contract_id=source.id,
                    source_assignment_head_id=source_assignment_id,
                )
                successor_assignment_record = build_assignment_fact(
                    assignment_context,
                    transition,
                    candidate=candidate,
                    receipt_id=receipt_id,
                    target_contract=successor,
                    target_declaration_record=declaration.records[0],
                    source_terminal_record=terminal,
                )
    else:
        assignment_mode = AssignmentMode.OVERRIDE
        expected_source_head = str(assignment_override["source_assignment_id"])
        if expected_source_head != source_assignment_id:
            raise ValueError("handoff source assignment head changed")
        assignment_worker_id = str(assignment_override["worker_id"])
        transition = AssignmentTransition(
            target_contract_id=successor.id,
            worker_id=assignment_worker_id,
            previous_assignment_id="",
            reason=reason,
            mode=AssignmentMode.OVERRIDE,
            source_contract_id=source.id,
            source_assignment_head_id=source_assignment_id,
        )
        successor_assignment_record = build_assignment_fact(
            assignment_context,
            transition,
            candidate=candidate,
            receipt_id=receipt_id,
            target_contract=successor,
            target_declaration_record=declaration.records[0],
            source_terminal_record=terminal,
        )
    if successor_assignment_record is not None:
        assignment_intents = (
            AssignmentIntent(
                successor_assignment_record.id,
                successor.id,
                declaration.records[0].id,
                assignment_mode,
            ),
        )
    hold_id = ""
    hold_record: RuntimeRecord | None = None
    if hold_reason:
        hold_record = create_runtime_record(
            "claim.hold.placed",
            {
                "actor_id": candidate.actor_id,
                "candidate_id": candidate.id,
                "contract_id": successor.id,
                "key_id": candidate.key_id,
                "reason": hold_reason,
            },
            causal_parents=(receipt_id,),
        )
        hold_id = hold_record.id
    copied_holds: tuple[RuntimeRecord, ...] = ()
    if edit_intent is not None and source_hold_records:
        copied_holds = tuple(
            create_runtime_record(
                "claim.hold.placed",
                {
                    "actor_id": candidate.actor_id,
                    "candidate_id": candidate.id,
                    "contract_id": successor.id,
                    "key_id": candidate.key_id,
                    "reason": str(source_hold.payload["reason"]),
                },
                causal_parents=(receipt_id, source_hold.id, declaration.records[0].id),
            )
            for source_hold in source_hold_records
        )
    edit_link = None
    if edit_intent is not None:
        edit_link = {
            "changed_fields": edit_intent.changed_fields,
            "source_declaration_id": source_declaration.id,
            "successor_declaration_id": declaration.records[0].id,
            "source_assignment_id": edit_intent.source_assignment_id,
            "source_hold_ids": edit_intent.source_hold_ids,
            "hold_mapping": tuple(
                {
                    "source_hold_id": source_hold.id,
                    "successor_hold_id": copied.id,
                }
                for source_hold, copied in zip(
                    source_hold_records, copied_holds, strict=True
                )
            ),
        }
    retained_ids = tuple(
        sorted(
            {asset_id for _, asset_ids in completed_outputs for asset_id in asset_ids}
        )
    )
    if inventory.expanded:
        child_terminal_ids = tuple(record.id for record in descendant_terminals)
        repair_terminal_ids = tuple(record.id for record in repair_terminals)
        return_ids = tuple(record.id for record in return_records)
        retained_proofs = _output_proofs(source.id, completed_outputs, context)
        proof_by_contract = {
            contract_id: [
                {
                    "output_name": output_name,
                    "asset_ids": asset_ids,
                    "proof_record_ids": proof_ids,
                }
                for output_name, asset_ids, proof_ids in outputs
            ]
            for contract_id, outputs in inventory.repair_outputs
        }
        handoff_payload = {
            "actor_id": candidate.actor_id,
            "allowance_returns": tuple(
                {
                    "donor_contract_id": str(item.payload["from_contract_id"]),
                    "amount": int(item.payload["amount"]),
                    "record_id": item.id,
                }
                for item in return_records
            ),
            "budget": budget,
            "candidate_id": candidate.id,
            "cancel_record_ids": child_terminal_ids,
            "cancelled_contract_ids": tuple(item.id for item in inventory.cancelled),
            "completed_outputs": tuple(sorted(satisfied_names)),
            "contract_id": source.id,
            "hold_id": hold_id,
            "key_id": candidate.key_id,
            "preserved_contract_ids": inventory.preserved,
            "reason": reason,
            "retained_output_proofs": retained_proofs,
            "repair_output_proofs": proof_by_contract,
            "repair_record_ids": repair_terminal_ids,
            "repair_contract_ids": tuple(item.id for item in inventory.repairs),
            "retained_asset_ids": retained_ids,
            "return_record_ids": return_ids,
            "returned_allowance": return_total,
            "source_original_balance": source_balance,
            "successor_id": successor.id,
        }
        link_parents = (
            receipt_id,
            terminal.id,
            declaration.records[0].id,
            *(
                fact_id
                for proof in retained_proofs.values()
                for fact_id in proof["proof_record_ids"]
            ),
            *child_terminal_ids,
            *repair_terminal_ids,
            *return_ids,
            *((hold_id,) if hold_id else ()),
        )
    else:
        # Keep the old leaf link payload and identity unchanged.
        handoff_payload = {
            "actor_id": candidate.actor_id,
            "budget": budget,
            "candidate_id": candidate.id,
            "completed_outputs": tuple(sorted(satisfied_names)),
            "contract_id": source.id,
            "hold_id": hold_id,
            "key_id": candidate.key_id,
            "reason": reason,
            "retained_asset_ids": retained_ids,
            "successor_id": successor.id,
        }
        link_parents = (
            receipt_id,
            terminal.id,
            declaration.records[0].id,
            *((hold_id,) if hold_id else ()),
        )
    if edit_link is not None:
        handoff_payload["edit"] = edit_link
        link_parents = (
            *link_parents,
            source_declaration.id,
            *(
                (edit_intent.source_assignment_id,)
                if edit_intent.source_assignment_id
                else ()
            ),
            *edit_intent.source_hold_ids,
            *(record.id for record in copied_holds),
        )
    if assignment_mode is not None and (
        source_assignment is not None or assignment_override is not None
    ):
        handoff_payload.update(
            {
                "assignment_mode": assignment_mode.value,
                "source_assignment_id": source_assignment_id,
                "assignment_fact_id": (
                    successor_assignment_record.id
                    if successor_assignment_record is not None
                    else ""
                ),
                "worker_id": assignment_worker_id,
            }
        )
    handoff = create_runtime_record(
        "task.handed_off",
        handoff_payload,
        causal_parents=tuple(
            dict.fromkeys(
                (
                    *link_parents,
                    *((source_assignment_id,) if source_assignment_id else ()),
                    *(
                        (successor_assignment_record.id,)
                        if successor_assignment_record
                        else ()
                    ),
                )
            )
        ),
    )
    records = (terminal, *descendant_terminals, *repair_terminals, *return_records)
    records += declaration.records
    records += copied_holds
    if hold_record is not None:
        records += (hold_record,)
    if successor_assignment_record is not None:
        records += (successor_assignment_record,)
    records += (handoff,)
    if assignment_mode is not None and (
        source_assignment is not None or assignment_override is not None
    ):
        successor_witnesses = (
            SuccessorAssignmentWitness(
                source_contract_id=source.id,
                source_assignment_head_id=source_assignment_id,
                source_terminal_record_id=terminal.id,
                successor_contract_id=successor.id,
                successor_declaration_record_id=declaration.records[0].id,
                successor_assignment_record_id=(
                    successor_assignment_record.id
                    if successor_assignment_record is not None
                    else ""
                ),
                successor_link_record_id=handoff.id,
                assignment_mode=assignment_mode,
            ),
        )
    required = ["contract.cancel", "contract.publish"]
    if edit_intent is not None:
        required.append("task.edit")
        if source_hold_records:
            required.append("task.block")
    if hold_record is not None:
        required.append("task.block")
    if assignment_override is not None:
        required.append("task.assignment.manage")
    return EffectProjection(
        records=records,
        relation_target=source.id,
        contract=successor,
        additional_capabilities=tuple(required),
        assignment_intents=assignment_intents,
        successor_assignment_witnesses=successor_witnesses,
    )


def project_task_handoffs(
    runtime_records: Iterable[RuntimeRecord],
    *,
    source_contract_id: str | None = None,
) -> tuple[RuntimeRecord, ...]:
    """Return durable handoff link facts in the supplied revision order."""
    return tuple(
        record
        for record in runtime_records
        if record.record_type == "task.handed_off"
        and (
            source_contract_id is None
            or record.payload.get("contract_id") == source_contract_id
        )
    )


def _successor_contract(
    source: Contract,
    outputs: tuple[str, ...],
    budget: int,
    reason: str,
) -> Contract:
    return build_successor_contract(
        source,
        outputs,
        budget,
        reason,
        name_suffix=".handoff",
    )


def _output_proofs(contract_id, slots, context) -> dict:
    result = {}
    for output_name, asset_ids in slots:
        proof_ids = {
            record.id
            for record in context.runtime_records
            if record.record_type == "asset.committed"
            and isinstance(record.payload.get("asset"), Mapping)
            and record.payload["asset"].get("id") in asset_ids
        }
        proof_ids.update(
            record.id
            for record in context.runtime_records
            if record.record_type == "output.qualified"
            and record.payload.get("contract_id") == contract_id
            and record.payload.get("output_name") == output_name
            and record.payload.get("asset_id") in asset_ids
        )
        result[output_name] = {
            "asset_ids": asset_ids,
            "proof_record_ids": tuple(sorted(proof_ids)),
        }
    return result


def _canonical_declaration(
    context: EffectProjectionContext, contract_id: str
) -> RuntimeRecord:
    matches = [
        record
        for record in context.runtime_records
        if record.record_type == "contract.declared"
        and isinstance(record.payload.get("contract"), Mapping)
        and record.payload["contract"].get("id") == contract_id
    ]
    contracts = [
        contract for contract in context.contracts if contract.id == contract_id
    ]
    if len(matches) != 1 or len(contracts) != 1:
        raise ValueError("task.handoff edit requires a unique declared source Contract")
    _validate_declaration_matches_contract(matches[0], contracts[0])
    return matches[0]


def _validate_declaration_matches_contract(
    declaration: RuntimeRecord, contract: Contract
) -> None:
    validate_runtime_record(declaration)
    raw = declaration.payload.get("contract")
    if not isinstance(raw, Mapping):
        raise ValueError("task.handoff Contract declaration is malformed")
    declared = contract_from_dict(deep_thaw(raw))
    if declared != contract:
        raise ValueError("task.handoff Contract declaration differs from projection")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"task.handoff {field} must be non-empty text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"task.handoff {field} is not valid UTF-8") from exc
    if len(encoded) > 65536:
        raise ValueError(f"task.handoff {field} exceeds 65536 UTF-8 bytes")
    return value


HANDOFF_EFFECTS = MappingProxyType(
    {
        "task.handoff": EffectHandler(
            capability="task.handoff",
            projector=project_task_handoff_effect,
            emitted_fact_types=frozenset({ASSIGNMENT_FACT}),
            assignment_admission=AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR,
            direct_output_fact_types=frozenset(
                {
                    "lifecycle.terminal",
                    "contract.declared",
                    ASSIGNMENT_FACT,
                    "claim.hold.placed",
                    "allowance.returned",
                    "task.handed_off",
                }
            ),
            reserved_fact_types=frozenset(),
        )
    }
)
