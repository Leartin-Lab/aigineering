"""Keep authentication failures outside claim commitment and replay paths."""

from __future__ import annotations

import ast
import inspect
import textwrap

from aigineering.core.candidate_rejection import (
    record_authenticated_candidate_rejection,
    record_unreceipted_authentication_rejection,
)
from aigineering.core.commitment import CandidateCommitter


def test_authentication_rejection_returns_through_audit_owner_before_commit_path():
    source = inspect.getsource(CandidateCommitter._commit_locked)
    function = ast.parse(textwrap.dedent(source)).body[0]
    auth_try = next(node for node in function.body if isinstance(node, ast.Try))
    assert "candidate_received_record(" in ast.unparse(auth_try)
    failure = ast.unparse(auth_try.handlers[0])
    assert "return record_candidate_rejection(" in failure
    assert "receipt=None" in failure
    assert "claim_binding" not in failure
    assert "close_claim_attempt" not in failure
    assert source.index("candidate_received_record(") < source.index(
        "read_candidate_publication("
    )
    assert source.index("read_candidate_publication(") < source.index(
        "close_claim_attempt("
    )
    assert source.index("close_claim_attempt(") < source.index("commit_ingress_batch(")
    for obsolete in ("effect_fact_owners", "assignment_intents", "validate_snapshot"):
        assert obsolete not in source

    auth_owner = ast.parse(
        textwrap.dedent(inspect.getsource(record_unreceipted_authentication_rejection))
    )
    owner = auth_owner.body[0]
    owner_text = ast.unparse(owner)
    assert "decision.accepted" in owner_text
    assert "candidate.authentication_rejected" in owner_text
    assert "candidate.received" in owner_text
    assert "record_candidate_rejection" in owner_text
    assert "candidate.claim_binding" not in owner_text
    assert "candidate.actor_id" not in owner_text
    assert any(
        isinstance(keyword.value, ast.Constant) and keyword.value.value is None
        for call in ast.walk(owner)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "record_candidate_rejection"
        for keyword in call.keywords
        if keyword.arg == "receipt"
    )

    authenticated_owner = ast.parse(
        textwrap.dedent(inspect.getsource(record_authenticated_candidate_rejection))
    ).body[0]
    authenticated_text = ast.unparse(authenticated_owner)
    assert "candidate.received" in authenticated_text
    assert "len(receipts) != 1" in authenticated_text
    assert "candidate_id" in authenticated_text
    assert "record_candidate_rejection" in authenticated_text
