"""Pure projection for signed, task-scoped collaboration comments."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType

from aigineering.core.claim_holds import open_claim_holds
from aigineering.core.injected_fact_replay import exact_single_effect_head_replay
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.projection_store_view import ProjectionStoreView
from aigineering.core.effect_projectors import EffectProjection
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.runtime_record import runtime_record_effective_payload
from aigineering.protocol.types import Contract
from aigineering.protocol.wire import contract_from_dict


def project_task_comment_effect(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    """Adapt pure comment semantics to the common effect projection contract."""
    record = project_task_comment(
        effect,
        candidate,
        receipt_id,
        contracts=context.contracts,
        runtime_records=context.runtime_records,
    )
    return EffectProjection(
        records=(record,),
        relation_target=str(record.payload["contract_id"]),
    )


def project_task_block_effect(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    """Project one signed hold placement into the generic claim-hold protocol."""
    payload = effect.payload
    if effect.effect_type != "task.block":
        raise ValueError("task block projector requires task.block")
    if candidate.effects != (effect,):
        raise ValueError("task.block Candidate must contain exactly one effect")
    if set(payload) != {"contract_id", "reason"}:
        raise ValueError("task.block payload fields must be contract_id, reason")
    contract_id = _required_text(payload["contract_id"], "contract_id")
    reason = _required_reason(payload["reason"])
    if not any(contract.id == contract_id for contract in context.contracts):
        raise ValueError(f"task.block references unknown Contract {contract_id!r}")

    record = create_runtime_record(
        "claim.hold.placed",
        {
            "actor_id": candidate.actor_id,
            "candidate_id": candidate.id,
            "contract_id": contract_id,
            "key_id": candidate.key_id,
            "reason": reason,
        },
        causal_parents=(receipt_id,),
    )
    existing = next(
        (item for item in context.runtime_records if item.id == record.id), None
    )
    if existing is not None:
        return EffectProjection((existing,), contract_id)

    if any(
        item.record_type == "lifecycle.terminal"
        and item.payload.get("contract_id") == contract_id
        for item in context.runtime_records
    ):
        raise ValueError("cannot block a terminal task")
    if any(
        item.record_type == "claim.granted"
        and item.payload.get("contract_id") == contract_id
        for item in context.runtime_records
    ):
        raise ValueError(
            "cannot block a task with claim history; active claims are never reset"
        )
    open_claim_holds(context.runtime_records, contract_id=contract_id)
    return EffectProjection((record,), contract_id)


def project_task_unblock_effect(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    """Project an exact release of one open hold."""
    payload = effect.payload
    if effect.effect_type != "task.unblock":
        raise ValueError("task unblock projector requires task.unblock")
    if candidate.effects != (effect,):
        raise ValueError("task.unblock Candidate must contain exactly one effect")
    if set(payload) != {"contract_id", "hold_id", "reason"}:
        raise ValueError(
            "task.unblock payload fields must be contract_id, hold_id, reason"
        )
    contract_id = _required_text(payload["contract_id"], "contract_id")
    hold_id = _required_text(payload["hold_id"], "hold_id")
    reason = _required_reason(payload["reason"])
    if not any(contract.id == contract_id for contract in context.contracts):
        raise ValueError(f"task.unblock references unknown Contract {contract_id!r}")

    record = create_runtime_record(
        "claim.hold.released",
        {
            "actor_id": candidate.actor_id,
            "candidate_id": candidate.id,
            "contract_id": contract_id,
            "hold_id": hold_id,
            "key_id": candidate.key_id,
            "reason": reason,
        },
        causal_parents=(receipt_id, hold_id),
    )
    existing = next(
        (item for item in context.runtime_records if item.id == record.id), None
    )
    if existing is not None:
        return EffectProjection((existing,), contract_id)

    open_holds = open_claim_holds(context.runtime_records, contract_id=contract_id)
    if not any(item.id == hold_id for item in open_holds):
        raise ValueError("task.unblock requires an open hold on the same Contract")
    return EffectProjection((record,), contract_id)


def project_task_promote_effect(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    """Release a task's exact complete hold set and append an audit fact."""
    if effect.effect_type != "task.promote":
        raise ValueError("task promote projector requires task.promote")
    if candidate.effects != (effect,):
        raise ValueError("task.promote Candidate must contain exactly one effect")
    contract, hold_ids, reason = _task_promote_request(effect, context)
    contract_id = contract.id
    declarations = [
        record
        for record in context.runtime_records
        if record.record_type == "contract.declared"
        and isinstance(record.payload.get("contract"), Mapping)
        and record.payload["contract"].get("id") == contract_id
    ]
    if (
        len(declarations) != 1
        or contract_from_dict(deep_thaw(declarations[0].payload["contract"]))
        != contract
    ):
        raise ValueError(
            "task.promote requires the exact canonical Contract declaration"
        )
    if candidate.causal_parents != (declarations[0].id, *hold_ids):
        raise ValueError(
            "task.promote Candidate must depend on declaration and exact holds"
        )

    releases = tuple(
        create_runtime_record(
            "claim.hold.released",
            {
                "actor_id": candidate.actor_id,
                "candidate_id": candidate.id,
                "contract_id": contract_id,
                "hold_id": hold_id,
                "key_id": candidate.key_id,
                "reason": reason,
            },
            causal_parents=(receipt_id, hold_id),
        )
        for hold_id in hold_ids
    )
    audit = create_runtime_record(
        "task.promoted",
        {
            "actor_id": candidate.actor_id,
            "candidate_id": candidate.id,
            "contract_id": contract_id,
            "hold_ids": list(hold_ids),
            "key_id": candidate.key_id,
            "reason": reason,
            "release_record_ids": [record.id for record in releases],
        },
        causal_parents=(
            receipt_id,
            *hold_ids,
            *(record.id for record in releases),
        ),
    )
    proposed = (*releases, audit)
    by_id = {record.id: record for record in context.runtime_records}
    replay_records = tuple(by_id.get(record.id) for record in proposed)
    if all(record is not None for record in replay_records):
        existing = tuple(record for record in replay_records if record is not None)
        if any(
            runtime_record_effective_payload(old)
            != runtime_record_effective_payload(new)
            for old, new in zip(existing, proposed, strict=True)
        ):
            raise ValueError("task.promote replay conflicts with durable facts")
        if not exact_single_effect_head_replay(
            candidate, receipt_id, existing, context.runtime_records
        ):
            raise ValueError("task.promote replay lacks its exact committed head")
        return EffectProjection(existing, contract_id)
    if any(record is not None for record in replay_records):
        raise ValueError(
            "task.promote history contains a partial or conflicting replay"
        )

    validate_task_promote_request(effect, context)
    return EffectProjection((*releases, audit), contract_id)


def _task_promote_request(effect: CandidateEffect, context: EffectProjectionContext):
    if set(effect.payload) != {"contract_id", "hold_ids", "reason"}:
        raise ValueError("task.promote payload fields are invalid")
    contract_id = _required_text(effect.payload["contract_id"], "contract_id")
    reason = _required_reason(effect.payload["reason"])
    hold_ids = effect.payload["hold_ids"]
    if not isinstance(hold_ids, (tuple, list)) or not 1 <= len(hold_ids) <= 64:
        raise ValueError("task.promote requires between 1 and 64 hold IDs")
    if any(not isinstance(item, str) or not item.strip() for item in hold_ids):
        raise ValueError("task.promote hold IDs must be non-empty strings")
    if tuple(hold_ids) != tuple(sorted(set(hold_ids))):
        raise ValueError("task.promote hold IDs must be sorted and unique")
    contract = next(
        (item for item in context.contracts if item.id == contract_id), None
    )
    if contract is None:
        raise ValueError(f"task.promote references unknown Contract {contract_id!r}")
    return contract, tuple(hold_ids), reason


def validate_task_promote_request(
    effect: CandidateEffect, context: EffectProjectionContext
) -> tuple[str, ...]:
    """Preflight a fresh promotion against one immutable projection snapshot."""
    if effect.effect_type != "task.promote":
        raise ValueError("task promote validation requires task.promote")
    contract, hold_ids, _reason = _task_promote_request(effect, context)
    contract_id = contract.id
    open_holds = open_claim_holds(context.runtime_records, contract_id=contract_id)
    if tuple(sorted(record.id for record in open_holds)) != hold_ids:
        raise ValueError("task.promote hold IDs must equal all currently open holds")
    if any(
        record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == contract_id
        for record in context.runtime_records
    ):
        raise ValueError("cannot promote a terminal task")
    claim_history = getattr(context, "claim_history_contract_ids", ())
    if contract_id in claim_history or any(
        record.record_type == "claim.granted"
        and record.payload.get("contract_id") == contract_id
        for record in context.runtime_records
    ):
        raise ValueError("cannot promote a task with claim history")
    if contract.outputs and all_outputs_satisfied(
        contract, ProjectionStoreView(context), require_outputs=True
    ):
        raise ValueError("cannot promote a task whose outputs are already satisfied")
    return hold_ids


def project_task_comment(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    *,
    contracts: Sequence[Contract],
    runtime_records: Sequence[RuntimeRecord],
) -> RuntimeRecord:
    """Project one authenticated task comment without Store access or writes."""
    if effect.effect_type != "task.comment":
        raise ValueError("task comment projector requires task.comment")
    payload = effect.payload
    if set(payload) != {"contract_id", "text", "reply_to"}:
        raise ValueError(
            "task.comment payload fields must be contract_id, text, reply_to"
        )
    contract_id = payload["contract_id"]
    text = payload["text"]
    reply_to = payload["reply_to"]
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("task.comment contract_id must be a non-empty string")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("task.comment text must be a non-empty string")
    try:
        text_size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError("task.comment text must be valid UTF-8") from exc
    if text_size > 64 * 1024:
        raise ValueError("task.comment text exceeds 65536 UTF-8 bytes")
    if not isinstance(reply_to, str):
        raise ValueError("task.comment reply_to must be a string")
    if not any(contract.id == contract_id for contract in contracts):
        raise ValueError(f"task.comment references unknown Contract {contract_id!r}")

    parents = [receipt_id]
    if reply_to:
        reply = next(
            (
                record
                for record in runtime_records
                if record.id == reply_to and record.record_type == "task.commented"
            ),
            None,
        )
        if reply is None:
            raise ValueError("task.comment reply_to must reference a committed comment")
        if reply.payload.get("contract_id") != contract_id:
            raise ValueError("task.comment reply_to must belong to the same Contract")
        parents.append(reply.id)

    return create_runtime_record(
        "task.commented",
        {
            "actor_id": candidate.actor_id,
            "candidate_id": candidate.id,
            "contract_id": contract_id,
            "key_id": candidate.key_id,
            "reply_to": reply_to,
            "text": text,
        },
        causal_parents=tuple(parents),
    )


def project_task_comments(
    runtime_records: Iterable[RuntimeRecord], *, contract_id: str | None = None
) -> tuple[RuntimeRecord, ...]:
    """Return committed comments in the supplied durable record order."""
    return tuple(
        record
        for record in runtime_records
        if record.record_type == "task.commented"
        and (contract_id is None or record.payload.get("contract_id") == contract_id)
    )


def project_task_holds(
    runtime_records: Iterable[RuntimeRecord], *, contract_id: str | None = None
) -> tuple[RuntimeRecord, ...]:
    """Return current open generic claim holds for task-facing projections."""
    return open_claim_holds(runtime_records, contract_id=contract_id)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"task collaboration {field} must be non-empty text")
    return value


def _required_reason(value: object) -> str:
    text = _required_text(value, "reason")
    try:
        size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError("task collaboration reason must be valid UTF-8") from exc
    if size > 64 * 1024:
        raise ValueError("task collaboration reason exceeds 65536 UTF-8 bytes")
    return text


COLLABORATION_EFFECTS = MappingProxyType(
    {
        "task.comment": EffectHandler(
            capability="task.comment",
            projector=project_task_comment_effect,
            direct_output_fact_types=frozenset({"task.commented"}),
            reserved_fact_types=frozenset(),
        ),
        "task.block": EffectHandler(
            capability="task.block",
            projector=project_task_block_effect,
            direct_output_fact_types=frozenset({"claim.hold.placed"}),
            reserved_fact_types=frozenset(),
        ),
        "task.unblock": EffectHandler(
            capability="task.unblock",
            projector=project_task_unblock_effect,
            direct_output_fact_types=frozenset({"claim.hold.released"}),
            reserved_fact_types=frozenset(),
        ),
        "task.promote": EffectHandler(
            capability="task.unblock",
            projector=project_task_promote_effect,
            direct_output_fact_types=frozenset(
                {"claim.hold.released", "task.promoted"}
            ),
            reserved_fact_types=frozenset({"task.promoted"}),
        ),
    }
)
