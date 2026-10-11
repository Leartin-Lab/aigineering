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
    source = inspect.getsource(CandidateCommitter.commit)
    function = ast.parse(textwrap.dedent(source)).body[0]
    audit_call = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "record_unreceipted_authentication_rejection"
    )
    guard = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "rejected"
    )
    guard_text = ast.unparse(guard)
    assert "is not None" in ast.unparse(guard.test)
    assert "self._trace_exporter.export(rejected.trace_entries)" in guard_text
    assert any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Name)
        and node.value.id == "rejected"
        for node in ast.walk(guard)
    )

    attempt_calls = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "close_claim_attempt"
    ]
    writer_argument_calls = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "commit_ingress_arguments"
    ]
    assert attempt_calls and writer_argument_calls
    assert audit_call.lineno < min(attempt_calls)
    assert audit_call.lineno < min(writer_argument_calls)

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
