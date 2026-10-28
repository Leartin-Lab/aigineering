"""Verified recovery-lineage read view tests."""

import json
import os
from dataclasses import replace

import click
import pytest
from click.testing import CliRunner
from conftest import hosted_worker

from aigineering.agent.llm import ProviderError
from aigineering.cli import task_lineage as lineage_cli
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.ids import contract_from_fields
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
    task_handoff_effect,
)
from aigineering.protocol.types import Asset
from aigineering.core.publication_history import index_published_records
from aigineering.plugins.task_attachments import validate_published_member
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.runtime_record import validate_runtime_record
from aigineering.runtime import (
    WorkerInvocationError,
    claim_next_package,
    execute_claimed_package,
)
from aigineering.task_lineage import _cycle_members, task_lineage
from aigineering.task_publication import (
    publish_task_contract,
    retry_local_task_result,
)
from aigineering.task_diagnostics import task_invocation_failure_binding
from aigineering.task_view_query import load_task_view_query_context
from aigineering.plugins.handoff import HANDOFF_EFFECTS
from aigineering.core.candidate_publisher import publish_effect
from test_redis_collaboration import MemoryRedis

from aigineering.cli.task import task_group


def _signed_handoff_lineage_fixture(tmp_path, variant):
    """Publish a real leaf, expanded, or edit handoff for lineage tests."""
    from aigineering.core.control_plane import build_control_plane_contract
    from aigineering.core.ids import contract_from_fields

    store = SQLiteStore(tmp_path / f"handoff-{variant}.sqlite")
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:lineage-handoff",
        "lineage-handoff-key",
        signer.kind,
        signer.signer_id,
        (
            "contract.cancel",
            "contract.publish",
            "task.handoff",
            "task.edit",
            "task.block",
            "asset.publish",
        ),
    )
    genesis = create_genesis_manifest("lineage-handoff", (actor,), "policy:test")
    initialize_genesis(store, genesis)
    source = build_control_plane_contract(
        name="lineage handoff source",
        description="source detail must remain private",
        outputs=("result",),
        budget=5,
    )
    if variant == "compound":
        from aigineering.core.candidate_publisher import publish_effects

        source_decision = publish_effects(
            store,
            store,
            genesis,
            actor,
            signer,
            (
                contract_declaration_effect(source),
                asset_proposal_effect(
                    Asset(
                        id="asset:lineage-source-note",
                        name="lineage source note",
                        content="compound source publication",
                        origin="human",
                    )
                ),
            ),
            idempotency_key="source-compound",
        )
    else:
        source_decision = publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            contract_declaration_effect(source),
            idempotency_key=f"source-{variant}",
        )
    assert source_decision.accepted
    source_declaration = next(
        record
        for record in source_decision.runtime_records
        if record.record_type == "contract.declared"
    )
    if variant == "expanded":
        child = contract_from_fields(
            name="lineage expanded child",
            description="child",
            parent_id=source.id,
            budget=1,
            outputs=(),
            inputs=(),
            activation="",
            tool_scope=(),
            labels=(),
            worker_capabilities=(),
            origin="human",
        )
        child_decision = publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            contract_declaration_effect(child),
            idempotency_key="expanded-child",
        )
        assert child_decision.accepted
    edit = None
    if variant == "edit":
        edit = {
            "source_assignment_id": "",
            "source_hold_ids": [],
            "title": "edited successor",
        }
    decision = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        task_handoff_effect(source.id, "continue safely", edit=edit),
        idempotency_key=f"handoff-{variant}",
        additional_effects=HANDOFF_EFFECTS,
        causal_parents=(source.id, source_declaration.id) if edit else (),
    )
    assert decision.accepted, decision.rejection_reason
    link = next(
        record
        for record in decision.runtime_records
        if record.record_type == "task.handed_off"
    )
    target_id = link.payload["successor_id"]
    assert store.get_contract(target_id) is not None
    return store, source.id, target_id


@pytest.mark.parametrize("variant", ("leaf", "expanded", "edit"))
def test_signed_handoff_lineage_is_bidirectional_exact_and_read_only(tmp_path, variant):
    store, source_id, target_id = _signed_handoff_lineage_fixture(tmp_path, variant)
    before_revision = store.get_runtime_revision()
    before_records = tuple(record.id for _, record in store.scan_runtime_records())

    source_view = task_lineage(store, source_id)["data"]
    target_view = task_lineage(store, target_id)["data"]

    assert source_view["contract_ids"] == target_view["contract_ids"]
    assert {source_id, target_id}.issubset(source_view["contract_ids"])
    assert source_view["edges"] == target_view["edges"]
    edges = [
        edge for edge in source_view["edges"] if edge["kind"] == "handoff_publication"
    ]
    assert len(edges) == 1
    edge = edges[0]
    assert (edge["source_contract_id"], edge["target_contract_id"]) == (
        source_id,
        target_id,
    )
    assert edge["handoff_link_id"] in edge["evidence_record_ids"]
    assert edge["handoff_receipt_id"] in edge["evidence_record_ids"]
    assert edge["handoff_head_id"] in edge["evidence_record_ids"]
    assert edge["cancelled_terminal_id"] in edge["evidence_record_ids"]
    assert edge["source_declaration_id"] in edge["evidence_record_ids"]
    assert edge["target_declaration_id"] in edge["evidence_record_ids"]
    assert edge["target_receipt_id"] in edge["evidence_record_ids"]
    assert edge["target_head_id"] in edge["evidence_record_ids"]
    witness_fields = (
        "source_declaration_id",
        "source_receipt_id",
        "source_head_id",
        "handoff_link_id",
        "handoff_receipt_id",
        "handoff_head_id",
        "cancelled_terminal_id",
        "target_declaration_id",
        "target_receipt_id",
        "target_head_id",
    )
    assert edge["evidence_record_ids"] == sorted(
        {edge[field] for field in witness_fields}
    )
    assert "handoff_publication" in source_view["covered_relations"]
    assert source_view["contracts"] == target_view["contracts"]

    class Collaboration:
        def sync(self):
            return None

    lineage_group = click.Group()
    lineage_cli.register_task_lineage_command(lineage_group)
    original_snapshot = lineage_cli._persistent_snapshot
    original_collaboration = lineage_cli.collaboration_service
    lineage_cli._persistent_snapshot = lambda: store.snapshot()
    lineage_cli.collaboration_service = lambda _store: Collaboration()
    try:
        rendered = CliRunner().invoke(lineage_group, ["lineage", source_id])
    finally:
        lineage_cli._persistent_snapshot = original_snapshot
        lineage_cli.collaboration_service = original_collaboration
    assert rendered.exit_code == 0, rendered.output
    for field in (
        "source_declaration_id",
        "source_receipt_id",
        "source_head_id",
        "handoff_link_id",
        "handoff_receipt_id",
        "handoff_head_id",
        "cancelled_terminal_id",
        "target_declaration_id",
        "target_receipt_id",
        "target_head_id",
    ):
        assert edge[field] in rendered.output
    assert "source detail must remain private" not in rendered.output
    assert "continue safely" not in rendered.output
    assert store.get_runtime_revision() == before_revision
    assert (
        tuple(record.id for _, record in store.scan_runtime_records()) == before_records
    )
    copied = store.snapshot()
    try:
        copied_view = task_lineage(copied, target_id)["data"]
        assert copied_view["edges"] == source_view["edges"]
        assert (
            tuple(record.id for _, record in copied.scan_runtime_records())
            == before_records
        )
    finally:
        copied.close()
    store.close()


def test_compound_published_source_has_verified_handoff_successor(tmp_path):
    store, source_id, target_id = _signed_handoff_lineage_fixture(tmp_path, "compound")
    result = task_lineage(store, source_id)["data"]
    handoff = next(
        edge for edge in result["edges"] if edge["kind"] == "handoff_publication"
    )
    assert (handoff["source_contract_id"], handoff["target_contract_id"]) == (
        source_id,
        target_id,
    )
    source_receipt = store.get_runtime_record(handoff["source_receipt_id"])
    assert len(source_receipt.payload["effect_types"]) > 1
    store.close()


def _signed_revision_lineage_fixture(tmp_path):
    from test_task_revision import _commit, _env

    from aigineering.plugins.task_revision import TASK_REVISION_EFFECTS
    from aigineering.plugins.task_review import TASK_REVIEW_EFFECTS
    from aigineering.protocol.effect_builders import task_revision_effect

    env = _env(tmp_path)
    result = _commit(
        env["store"],
        env["genesis"],
        env["signer"],
        env["operator"],
        task_revision_effect(env["decision"].id, "Address the reviewed changes"),
        "lineage-revision",
        effects={**TASK_REVIEW_EFFECTS, **TASK_REVISION_EFFECTS},
    )
    assert result.accepted, result
    link = next(
        record
        for record in result.runtime_records
        if record.record_type == "task.revised"
    )
    return env["store"], env["source"].id, link.payload["successor_id"]


def test_signed_revision_lineage_is_bidirectional_exact_and_read_only(
    tmp_path, monkeypatch
):
    store, source_id, target_id = _signed_revision_lineage_fixture(tmp_path)
    before_revision = store.get_runtime_revision()
    before_ids = tuple(record.id for _revision, record in store.scan_runtime_records())

    from_source = task_lineage(store, source_id)["data"]
    from_target = task_lineage(store, target_id)["data"]
    assert from_source["contract_ids"] == from_target["contract_ids"]
    assert from_source["contracts"] == from_target["contracts"]
    assert from_source["edges"] == from_target["edges"]
    edge = next(
        edge for edge in from_source["edges"] if edge["kind"] == "revision_publication"
    )
    assert (edge["source_contract_id"], edge["target_contract_id"]) == (
        source_id,
        target_id,
    )
    witness_fields = (
        "source_declaration_id",
        "source_receipt_id",
        "source_head_id",
        "revision_link_id",
        "revision_receipt_id",
        "revision_head_id",
        "cancelled_terminal_id",
        "target_declaration_id",
        "target_receipt_id",
        "target_head_id",
    )
    assert edge["evidence_record_ids"] == sorted(
        {edge[field] for field in witness_fields}
    )
    assert "revision_publication" in from_source["covered_relations"]
    assert store.get_runtime_revision() == before_revision
    assert (
        tuple(record.id for _revision, record in store.scan_runtime_records())
        == before_ids
    )

    class Collaboration:
        def sync(self):
            return None

    lineage_group = click.Group()
    lineage_cli.register_task_lineage_command(lineage_group)
    monkeypatch.setattr(lineage_cli, "_persistent_snapshot", lambda: store.snapshot())
    monkeypatch.setattr(
        lineage_cli, "collaboration_service", lambda _store: Collaboration()
    )
    json_result = CliRunner().invoke(lineage_group, ["lineage", source_id, "--json"])
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output)["data"]["edges"] == from_source["edges"]
    human_result = CliRunner().invoke(lineage_group, ["lineage", source_id])
    assert human_result.exit_code == 0, human_result.output
    for field in witness_fields:
        assert edge[field] in human_result.output
    assert "Address the reviewed changes" not in human_result.output
    copied = store.snapshot()
    try:
        assert task_lineage(copied, target_id)["data"]["edges"] == from_source["edges"]
    finally:
        copied.close()
    store.close()


def test_revision_effect_fence_runs_after_generic_publication_proof(
    tmp_path, monkeypatch
):
    from aigineering.core.publication_history import validate_published_member
    from aigineering.plugins.task_revision import validate_task_revision_relations

    store, source_id, target_id = _signed_revision_lineage_fixture(tmp_path)
    query, records, index, link = _query_with_coherent_successor_corruption(
        store, "wrong_effect", link_record_type="task.revised"
    )
    validate_published_member(
        records,
        link,
        domain_id=query.domain_id,
        allow_additional_causal_parents=True,
        record_index=index,
    )
    witnesses, issues = validate_task_revision_relations(query, records, index)
    assert witnesses == ()
    assert len(issues) == 1
    assert issues[0].code == "revision_publication_unverified"
    assert (issues[0].source_contract_id, issues[0].target_contract_id) == (
        source_id,
        target_id,
    )

    monkeypatch.setattr(
        "aigineering.task_lineage.load_task_view_query_context", lambda _store: query
    )
    for endpoint in (source_id, target_id):
        data = task_lineage(store, endpoint)["data"]
        assert not any(edge["kind"] == "revision_publication" for edge in data["edges"])
        assert endpoint in data["contract_ids"]
        assert any(
            issue["code"] == "revision_publication_unverified"
            and issue["source_contract_id"] == source_id
            and issue["target_contract_id"] == target_id
            for issue in data["issues"]
        )
    assert "Address the reviewed changes" not in repr(data)
    store.close()


@pytest.mark.parametrize("endpoint", ("source", "target"))
def test_revision_endpoint_materialization_mismatch_is_visible_without_expansion(
    tmp_path, monkeypatch, endpoint
):
    from aigineering.core.publication_history import validate_published_member
    from aigineering.plugins.task_revision import validate_task_revision_relations
    import aigineering.task_lineage as lineage_module

    store, source_id, target_id = _signed_revision_lineage_fixture(tmp_path)
    query = load_task_view_query_context(store)
    records = tuple(record for _revision, record in query.runtime_records)
    index = index_published_records(records)
    link = next(record for record in records if record.record_type == "task.revised")

    # The signed publication remains generically valid. Only the captured
    # materialized Contract endpoint is changed, preserving its ID.
    validate_published_member(
        records,
        link,
        domain_id=query.domain_id,
        allow_additional_causal_parents=True,
        record_index=index,
    )
    for contract_id in (source_id, target_id):
        declaration = next(
            record
            for record in records
            if record.record_type == "contract.declared"
            and record.payload["contract"]["id"] == contract_id
        )
        validate_published_member(
            records,
            declaration,
            domain_id=query.domain_id,
            record_index=index,
        )

    original = next(
        contract
        for contract in query.contracts
        if contract.id == (source_id if endpoint == "source" else target_id)
    )
    changed = replace(original, description=f"{original.description} changed")
    changed_contracts = tuple(
        changed if contract.id == original.id else contract
        for contract in query.contracts
    )
    changed_query = replace(query, contracts=changed_contracts)
    witnesses, issues = validate_task_revision_relations(changed_query, records, index)
    assert witnesses == ()
    assert len(issues) == 1
    assert issues[0].code == "revision_publication_unverified"
    assert (issues[0].source_contract_id, issues[0].target_contract_id) == (
        source_id,
        target_id,
    )

    monkeypatch.setattr(
        lineage_module, "load_task_view_query_context", lambda _store: changed_query
    )
    # The modified materialization is intentionally not a canonical task-view
    # Contract. Isolate the lineage composer so this test measures its safe
    # issue and component behavior after the feature fence, not task-view's
    # separate snapshot-membership guard.
    monkeypatch.setattr(
        lineage_module,
        "project_task_status",
        lambda contract, _store, **_kwargs: {"contract_id": contract.id},
    )
    before_revision = store.get_runtime_revision()
    before_records = tuple(store.scan_runtime_records())
    for selected_id, other_id in ((source_id, target_id), (target_id, source_id)):
        data = task_lineage(store, selected_id)["data"]
        assert not any(edge["kind"] == "revision_publication" for edge in data["edges"])
        assert selected_id in data["contract_ids"]
        assert other_id not in data["contract_ids"]
        assert any(
            issue["code"] == "revision_publication_unverified"
            and issue["source_contract_id"] == source_id
            and issue["target_contract_id"] == target_id
            for issue in data["issues"]
        )
    assert tuple(store.scan_runtime_records()) == before_records
    assert store.get_runtime_revision() == before_revision
    store.close()


def test_duplicate_materialized_contract_id_fails_lineage_before_component_output(
    tmp_path, monkeypatch
):
    from aigineering.task_lineage import TaskLineageError
    import aigineering.task_lineage as lineage_module

    store, source_id, _target_id = _signed_revision_lineage_fixture(tmp_path)
    query = load_task_view_query_context(store)
    duplicate = next(
        contract for contract in query.contracts if contract.id == source_id
    )
    changed_query = replace(query, contracts=(*query.contracts, duplicate))
    monkeypatch.setattr(
        lineage_module, "load_task_view_query_context", lambda _store: changed_query
    )
    before_revision = store.get_runtime_revision()
    before_records = tuple(store.scan_runtime_records())
    with pytest.raises(TaskLineageError) as exc_info:
        task_lineage(store, source_id)
    assert exc_info.value.code == "lineage_integrity_error"
    assert tuple(store.scan_runtime_records()) == before_records
    assert store.get_runtime_revision() == before_revision
    store.close()


@pytest.mark.parametrize(
    ("corruption", "generic_link_passes"),
    [
        ("missing_terminal", True),
        ("duplicate_terminal", True),
        ("wrong_source_terminal", True),
        ("wrong_kind_terminal", True),
        ("wrong_terminal_state", True),
        ("missing_terminal_parent", True),
        ("target_declaration_direct_parent_missing", True),
        ("target_declaration_not_in_head", True),
        ("wrong_effect", True),
        ("wrong_domain", True),
        ("wrong_candidate", False),
        ("missing_receipt", False),
        ("duplicate_receipt", False),
        ("missing_head", False),
        ("duplicate_head", False),
    ],
)
def test_revision_relation_fences_preserve_generic_proof_ownership(
    tmp_path, corruption, generic_link_passes
):
    from aigineering.core.publication_history import validate_published_member
    from aigineering.plugins.task_revision import validate_task_revision_relations

    store, source_id, target_id = _signed_revision_lineage_fixture(tmp_path)
    query, records, index, link = _query_with_coherent_successor_corruption(
        store, corruption, link_record_type="task.revised"
    )
    if generic_link_passes:
        validate_published_member(
            records,
            link,
            domain_id=None if corruption == "wrong_domain" else query.domain_id,
            allow_additional_causal_parents=True,
            record_index=index,
        )
        if corruption == "target_declaration_not_in_head":
            target_declaration = next(
                record
                for record in records
                if record.record_type == "contract.declared"
                and record.payload["contract"]["id"] == target_id
            )
            with pytest.raises(ValueError):
                validate_published_member(
                    records,
                    target_declaration,
                    domain_id=query.domain_id,
                    record_index=index,
                )
    else:
        with pytest.raises(ValueError):
            validate_published_member(
                records,
                link,
                domain_id=query.domain_id,
                allow_additional_causal_parents=True,
                record_index=index,
            )

    witnesses, issues = validate_task_revision_relations(query, records, index)
    assert witnesses == ()
    assert len(issues) == 1
    assert issues[0].code == "revision_publication_unverified"
    assert (issues[0].source_contract_id, issues[0].target_contract_id) == (
        source_id,
        target_id,
    )
    assert "Address the reviewed changes" not in repr(issues)
    store.close()


def test_mixed_handoff_revision_pair_is_ambiguous_without_a_winning_edge(
    tmp_path, monkeypatch
):
    from aigineering.plugins.handoff import HandoffPublicationWitness
    from aigineering.plugins.task_revision import validate_task_revision_relations
    import aigineering.task_lineage as lineage_module

    store, source_id, target_id = _signed_revision_lineage_fixture(tmp_path)
    query = load_task_view_query_context(store)
    records = tuple(record for _revision, record in query.runtime_records)
    index = index_published_records(records)
    revision_witnesses, revision_issues = validate_task_revision_relations(
        query, records, index
    )
    assert len(revision_witnesses) == 1
    assert not revision_issues
    revision = revision_witnesses[0]
    monkeypatch.setattr(
        lineage_module,
        "validate_task_handoff_relations",
        lambda *_args: (
            (
                HandoffPublicationWitness(
                    source_contract_id=source_id,
                    target_contract_id=target_id,
                    source_declaration_id=revision.source_declaration_id,
                    source_receipt_id=revision.source_receipt_id,
                    source_head_id=revision.source_head_id,
                    handoff_link_id=revision.revision_link_id,
                    handoff_receipt_id=revision.revision_receipt_id,
                    handoff_head_id=revision.revision_head_id,
                    cancelled_terminal_id=revision.cancelled_terminal_id,
                    target_declaration_id=revision.target_declaration_id,
                    target_receipt_id=revision.target_receipt_id,
                    target_head_id=revision.target_head_id,
                ),
            ),
            (),
        ),
    )

    data = task_lineage(store, source_id)["data"]
    assert not any(
        edge["kind"] in {"handoff_publication", "revision_publication"}
        for edge in data["edges"]
    )
    assert {source_id, target_id}.issubset(data["contract_ids"])
    assert {
        issue["code"]
        for issue in data["issues"]
        if issue.get("source_contract_id") == source_id
        and issue.get("target_contract_id") == target_id
    } >= {"handoff_publication_ambiguous", "revision_publication_ambiguous"}
    store.close()


def test_handoff_source_and_target_materialization_must_match_exact_declarations(
    tmp_path,
):
    from dataclasses import replace

    from aigineering.plugins.handoff import validate_task_handoff_relations

    store, source_id, target_id = _signed_handoff_lineage_fixture(tmp_path, "leaf")
    query = load_task_view_query_context(store)
    records = tuple(record for _revision, record in query.runtime_records)
    index = index_published_records(records)
    tampered = tuple(
        replace(contract, description="uncommitted materialization")
        if contract.id == source_id
        else contract
        for contract in query.contracts
    )
    changed_query = replace(query, contracts=tampered)

    witnesses, issues = validate_task_handoff_relations(changed_query, records, index)

    assert witnesses == ()
    assert len(issues) == 1
    assert issues[0].code == "handoff_publication_unverified"
    assert (issues[0].source_contract_id, issues[0].target_contract_id) == (
        source_id,
        target_id,
    )
    assert "uncommitted materialization" not in repr(issues)
    store.close()


@pytest.mark.parametrize("endpoint", ("source", "target"))
def test_handoff_materialization_mismatch_rejects_each_endpoint(tmp_path, endpoint):
    from aigineering.plugins.handoff import validate_task_handoff_relations

    store, source_id, target_id = _signed_handoff_lineage_fixture(
        tmp_path, f"materialization-{endpoint}"
    )
    query = load_task_view_query_context(store)
    records = tuple(record for _revision, record in query.runtime_records)
    index = index_published_records(records)
    corrupted_id = source_id if endpoint == "source" else target_id
    changed_query = replace(
        query,
        contracts=tuple(
            replace(contract, description="private materialization mismatch")
            if contract.id == corrupted_id
            else contract
            for contract in query.contracts
        ),
    )
    for contract_id in (source_id, target_id):
        declaration = next(
            record
            for record in records
            if record.record_type == "contract.declared"
            and record.payload["contract"]["id"] == contract_id
        )
        # Generic receipt/head/member publication remains valid; the handoff
        # owner separately enforces declaration/materialization equality.
        validate_published_member(
            records,
            declaration,
            domain_id=query.domain_id,
            record_index=index,
        )
    witnesses, issues = validate_task_handoff_relations(changed_query, records, index)
    assert witnesses == ()
    assert len(issues) == 1
    assert issues[0].code == "handoff_publication_unverified"
    assert "private materialization mismatch" not in repr(issues)
    store.close()


@pytest.mark.parametrize(
    "corruption",
    (
        "missing_source_declaration",
        "duplicate_target_declaration",
        "malformed_link_id",
        "foreign_index",
    ),
)
def test_malformed_handoff_history_returns_only_safe_unverified_issues(
    tmp_path, corruption
):
    from dataclasses import replace

    from aigineering.plugins.handoff import validate_task_handoff_relations

    store, source_id, target_id = _signed_handoff_lineage_fixture(
        tmp_path, f"malformed-{corruption}"
    )
    query = load_task_view_query_context(store)
    records = tuple(record for _revision, record in query.runtime_records)
    source_declaration = next(
        record
        for record in records
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == source_id
    )
    target_declaration = next(
        record
        for record in records
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == target_id
    )
    link = next(record for record in records if record.record_type == "task.handed_off")
    supplied_index = None
    if corruption == "missing_source_declaration":
        rows = tuple(
            (revision, record)
            for revision, record in query.runtime_records
            if record is not source_declaration
        )
    elif corruption == "duplicate_target_declaration":
        rows = (*query.runtime_records, (query.revision, target_declaration))
    elif corruption == "malformed_link_id":
        rows = tuple(
            (
                revision,
                replace(record, id="private-marker-not-a-runtime-id")
                if record is link
                else record,
            )
            for revision, record in query.runtime_records
        )
    else:
        rows = query.runtime_records
        supplied_index = index_published_records(tuple(reversed(records)))
    changed_query = replace(query, runtime_records=tuple(rows))
    records = tuple(record for _revision, record in changed_query.runtime_records)
    index = supplied_index or index_published_records(records)

    witnesses, issues = validate_task_handoff_relations(changed_query, records, index)

    assert witnesses == ()
    assert len(issues) == 1
    assert issues[0].code == "handoff_publication_unverified"
    assert (issues[0].source_contract_id, issues[0].target_contract_id) == (
        source_id,
        target_id,
    )
    assert "private-marker-not-a-runtime-id" not in repr(issues)
    assert "source detail must remain private" not in repr(issues)
    store.close()


def _query_with_extra_handoff_link(store, *, different_target):
    from aigineering.core.ids import contract_from_fields
    from aigineering.protocol.wire import contract_to_dict

    query = load_task_view_query_context(store)
    original_rows = tuple(query.runtime_records)
    records = tuple(record for _revision, record in original_rows)
    index = index_published_records(records)
    link = next(record for record in records if record.record_type == "task.handed_off")
    receipt, head = validate_published_member(
        records,
        link,
        domain_id=query.domain_id,
        allow_additional_causal_parents=True,
        record_index=index,
    )
    source_id = link.payload["contract_id"]
    target_declaration = next(
        record
        for record in records
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == link.payload["successor_id"]
    )
    if different_target:
        second_target = contract_from_fields(
            name="unlinked synthetic handoff target",
            description="synthetic target for captured-history test",
            inputs=(),
            outputs=(),
            activation="",
            budget=1,
            tool_scope=(),
            labels=(),
            origin="human",
            parent_id=None,
        )
        declaration_payload = deep_thaw(target_declaration.payload)
        declaration_payload["contract"] = contract_to_dict(second_target)
        second_declaration = create_runtime_record(
            "contract.declared",
            declaration_payload,
            causal_parents=(receipt.id,),
        )
        link_payload = deep_thaw(link.payload)
        link_payload["successor_id"] = second_target.id
        link_parents = tuple(
            parent for parent in link.causal_parents if parent != target_declaration.id
        ) + (second_declaration.id,)
        extra_target = second_target
    else:
        second_declaration = None
        link_payload = deep_thaw(link.payload)
        link_parents = (*link.causal_parents, target_declaration.id)
        extra_target = None
    extra_link = create_runtime_record(
        "task.handed_off",
        link_payload,
        causal_parents=link_parents,
    )
    member_ids = tuple(head.payload["committed_record_ids"])
    additions = tuple(
        item.id for item in (second_declaration, extra_link) if item is not None
    )
    head_payload = deep_thaw(head.payload)
    head_payload["committed_record_ids"] = [*member_ids, *additions]
    extra_head = create_runtime_record(
        "candidate.committed",
        head_payload,
        causal_parents=(receipt.id, *member_ids, *additions),
        recorded_at=head.recorded_at,
    )
    new_rows = tuple(
        (revision, record) for revision, record in original_rows if record is not head
    )
    extras = tuple(
        (query.revision, item)
        for item in (second_declaration, extra_link, extra_head)
        if item is not None
    )
    changed_query = replace(
        query,
        runtime_records=(*new_rows, *extras),
        contracts=(*query.contracts, *((extra_target,) if extra_target else ())),
    )
    changed_records = tuple(
        record for _revision, record in changed_query.runtime_records
    )
    changed_index = index_published_records(changed_records)
    links = tuple(
        record
        for record in changed_records
        if record.record_type == "task.handed_off"
        and record.payload.get("candidate_id") == link.payload["candidate_id"]
    )
    for candidate_link in links:
        validate_published_member(
            changed_records,
            candidate_link,
            domain_id=changed_query.domain_id,
            allow_additional_causal_parents=True,
            record_index=changed_index,
        )
    if second_declaration is not None:
        validate_published_member(
            changed_records,
            second_declaration,
            domain_id=changed_query.domain_id,
            record_index=changed_index,
        )
    return changed_query, changed_records, changed_index, source_id, extra_target


def _query_with_malformed_handoff_effect_types(store):
    """Build a hash-valid receipt/head whose effect_types is a Mapping."""
    query = load_task_view_query_context(store)
    original_rows = tuple(query.runtime_records)
    records = tuple(record for _revision, record in original_rows)
    index = index_published_records(records)
    link = next(record for record in records if record.record_type == "task.handed_off")
    receipt, head = validate_published_member(
        records,
        link,
        domain_id=query.domain_id,
        allow_additional_causal_parents=True,
        record_index=index,
    )
    payload = deep_thaw(receipt.payload)
    payload["effect_types"] = {"task.handoff": "task.handoff"}
    malformed_receipt = create_runtime_record(
        "candidate.received",
        payload,
        causal_parents=receipt.causal_parents,
        recorded_at=receipt.recorded_at,
    )
    remap = {receipt.id: malformed_receipt.id}
    replacements = []
    for record_id in head.payload["committed_record_ids"]:
        member = next(row for row in records if row.id == record_id)
        parents = tuple(remap.get(parent, parent) for parent in member.causal_parents)
        rebuilt = create_runtime_record(
            member.record_type,
            deep_thaw(member.payload),
            causal_parents=parents,
            recorded_at=member.recorded_at,
        )
        remap[member.id] = rebuilt.id
        replacements.append(rebuilt)
    rebuilt_link = next(
        row for row in replacements if row.record_type == "task.handed_off"
    )
    assert malformed_receipt.id in rebuilt_link.causal_parents
    member_ids = [
        remap[record_id] for record_id in head.payload["committed_record_ids"]
    ]
    malformed_head = create_runtime_record(
        "candidate.committed",
        {**deep_thaw(head.payload), "committed_record_ids": member_ids},
        causal_parents=(malformed_receipt.id, *member_ids),
        recorded_at=head.recorded_at,
    )
    replacement_ids = {
        receipt.id,
        head.id,
        *head.payload["committed_record_ids"],
    }
    rows = tuple(
        (revision, record)
        for revision, record in original_rows
        if record.id not in replacement_ids
    )
    rows += tuple(
        (query.revision, record)
        for record in (malformed_receipt, *replacements, malformed_head)
    )
    changed_query = replace(query, runtime_records=rows)
    changed_records = tuple(record for _revision, record in rows)
    changed_index = index_published_records(changed_records)
    malformed_link = next(
        record
        for record in changed_records
        if record.record_type == "task.handed_off"
        and record.payload.get("candidate_id") == link.payload.get("candidate_id")
    )
    # The shared receipt/head/member proof accepts the coherent records. The
    # feature owner must still reject the non-sequence effect_types container.
    validate_published_member(
        changed_records,
        malformed_link,
        domain_id=query.domain_id,
        allow_additional_causal_parents=True,
        record_index=changed_index,
    )
    return changed_query, changed_records, changed_index


def _query_with_coherent_handoff_corruption(store, corruption):
    """Rebuild one accepted handoff head around a single relation mutation."""
    return _query_with_coherent_successor_corruption(
        store, corruption, link_record_type="task.handed_off"
    )


def _query_with_coherent_successor_corruption(store, corruption, *, link_record_type):
    """Rebuild an accepted successor head while keeping generic proof coherent."""
    query = load_task_view_query_context(store)
    original_rows = tuple(query.runtime_records)
    original_records = tuple(record for _revision, record in original_rows)
    original_index = index_published_records(original_records)
    link = next(row for row in original_records if row.record_type == link_record_type)
    receipt, head = validate_published_member(
        original_records,
        link,
        domain_id=query.domain_id,
        allow_additional_causal_parents=True,
        record_index=original_index,
    )
    original_members = [
        next(row for row in original_records if row.id == record_id)
        for record_id in head.payload["committed_record_ids"]
    ]
    source_id = link.payload["contract_id"]
    target_id = link.payload["successor_id"]
    source_terminal = next(
        row
        for row in original_members
        if row.record_type == "lifecycle.terminal"
        and row.payload.get("contract_id") == source_id
    )
    target_declaration = next(
        row
        for row in original_members
        if row.record_type == "contract.declared"
        and row.payload["contract"]["id"] == target_id
    )

    receipt_payload = deep_thaw(receipt.payload)
    if corruption == "wrong_effect":
        receipt_payload["effect_types"] = ["task.block"]
    elif corruption == "wrong_domain":
        receipt_payload["domain_id"] = "private-wrong-domain-marker"
    effective_receipt = receipt
    if corruption in {"wrong_effect", "wrong_domain"}:
        effective_receipt = create_runtime_record(
            receipt.record_type,
            receipt_payload,
            causal_parents=receipt.causal_parents,
            recorded_at=receipt.recorded_at,
        )

    old_to_new = {receipt.id: effective_receipt.id}
    altered_members = {}
    extra_members = []
    if corruption == "duplicate_terminal":
        duplicate_payload = deep_thaw(source_terminal.payload)
        duplicate_payload["diagnostic_copy"] = True
        extra_members.append(
            create_runtime_record(
                source_terminal.record_type,
                duplicate_payload,
                causal_parents=tuple(
                    old_to_new.get(parent, parent)
                    for parent in source_terminal.causal_parents
                ),
                recorded_at=source_terminal.recorded_at,
            )
        )
    elif corruption == "wrong_source_terminal":
        altered_members[source_terminal.id] = create_runtime_record(
            source_terminal.record_type,
            {**deep_thaw(source_terminal.payload), "contract_id": target_id},
            causal_parents=source_terminal.causal_parents,
            recorded_at=source_terminal.recorded_at,
        )
    elif corruption == "wrong_kind_terminal":
        altered_members[source_terminal.id] = create_runtime_record(
            "task.terminal_observation",
            deep_thaw(source_terminal.payload),
            causal_parents=source_terminal.causal_parents,
            recorded_at=source_terminal.recorded_at,
        )
    elif corruption == "wrong_terminal_state":
        altered_members[source_terminal.id] = create_runtime_record(
            source_terminal.record_type,
            {**deep_thaw(source_terminal.payload), "terminal": "complete"},
            causal_parents=source_terminal.causal_parents,
            recorded_at=source_terminal.recorded_at,
        )

    rebuilt_members = []
    for member in original_members:
        if member is link:
            continue
        if corruption == "missing_terminal" and member.id == source_terminal.id:
            continue
        if (
            corruption == "target_declaration_not_in_head"
            and member.id == target_declaration.id
        ):
            continue
        replacement = altered_members.get(member.id, member)
        parents = tuple(
            old_to_new.get(parent, parent) for parent in replacement.causal_parents
        )
        rebuilt = create_runtime_record(
            replacement.record_type,
            deep_thaw(replacement.payload),
            causal_parents=parents,
            recorded_at=replacement.recorded_at,
        )
        old_to_new[member.id] = rebuilt.id
        rebuilt_members.append(rebuilt)
    rebuilt_members.extend(extra_members)

    link_payload = deep_thaw(link.payload)
    if corruption == "wrong_candidate":
        link_payload["candidate_id"] = "candidate:private-wrong-candidate-marker"
    link_parents = [old_to_new.get(parent, parent) for parent in link.causal_parents]
    if corruption in {"missing_terminal", "missing_terminal_parent"}:
        terminal_id = old_to_new.get(source_terminal.id, source_terminal.id)
        link_parents = [parent for parent in link_parents if parent != terminal_id]
    elif corruption == "duplicate_terminal":
        link_parents.extend(extra_members[0].id for _ in (0,))
    elif corruption == "wrong_source_terminal":
        link_parents = [
            old_to_new.get(parent, parent)
            for parent in link.causal_parents
            if parent != source_terminal.id
        ] + [old_to_new[source_terminal.id]]
    elif corruption == "target_declaration_not_in_head":
        link_parents = [
            parent
            for parent in link_parents
            if parent != old_to_new.get(target_declaration.id, target_declaration.id)
        ]
    elif corruption == "target_declaration_direct_parent_missing":
        link_parents = [
            parent
            for parent in link_parents
            if parent != old_to_new.get(target_declaration.id, target_declaration.id)
        ]
    rebuilt_link = create_runtime_record(
        link.record_type,
        link_payload,
        causal_parents=tuple(link_parents),
        recorded_at=link.recorded_at,
    )
    old_to_new[link.id] = rebuilt_link.id
    member_order = []
    for original_id in head.payload["committed_record_ids"]:
        if corruption == "missing_terminal" and original_id == source_terminal.id:
            continue
        if (
            corruption == "target_declaration_not_in_head"
            and original_id == target_declaration.id
        ):
            continue
        member_order.append(
            rebuilt_link.id
            if original_id == link.id
            else old_to_new.get(original_id, original_id)
        )
    if corruption == "duplicate_terminal":
        member_order.append(extra_members[0].id)
    rebuilt_head = create_runtime_record(
        head.record_type,
        {**deep_thaw(head.payload), "committed_record_ids": member_order},
        causal_parents=(effective_receipt.id, *member_order),
        recorded_at=head.recorded_at,
    )
    replaced_ids = {receipt.id, head.id, *(member.id for member in original_members)}
    changed_rows = tuple(
        (revision, record)
        for revision, record in original_rows
        if record.id not in replaced_ids
    )
    rebuilt_rows = [effective_receipt, *rebuilt_members, rebuilt_link, rebuilt_head]
    if corruption == "missing_receipt":
        rebuilt_rows.remove(effective_receipt)
    elif corruption == "missing_head":
        rebuilt_rows.remove(rebuilt_head)
    changed_rows += tuple((query.revision, record) for record in rebuilt_rows)
    if corruption == "duplicate_receipt":
        changed_rows += ((query.revision, effective_receipt),)
    elif corruption == "duplicate_head":
        changed_rows += ((query.revision, rebuilt_head),)
    if corruption == "target_declaration_not_in_head":
        changed_rows += ((query.revision, target_declaration),)
    changed_query = replace(query, runtime_records=changed_rows)
    changed_records = tuple(record for _revision, record in changed_rows)
    changed_index = index_published_records(changed_records)
    current_link = next(
        record
        for record in changed_records
        if record.record_type == link_record_type
        and record.payload.get("contract_id") == source_id
        and record.payload.get("successor_id") == target_id
    )
    return changed_query, changed_records, changed_index, current_link


@pytest.mark.parametrize("different_target", (False, True))
def test_one_handoff_candidate_head_cannot_publish_multiple_typed_links(
    tmp_path, monkeypatch, different_target
):
    from aigineering.plugins.handoff import validate_task_handoff_relations
    import aigineering.task_lineage as lineage_module

    store, _source_id, _target_id = _signed_handoff_lineage_fixture(
        tmp_path, f"duplicate-link-{different_target}"
    )
    before_revision = store.get_runtime_revision()
    before_records = tuple(store.scan_runtime_records())
    query, records, index, source_id, extra_target = _query_with_extra_handoff_link(
        store, different_target=different_target
    )
    witnesses, issues = validate_task_handoff_relations(query, records, index)
    assert witnesses == ()
    assert len(issues) == 2
    assert all(issue.code == "handoff_publication_unverified" for issue in issues)
    assert all(issue.source_contract_id == source_id for issue in issues)
    assert all(issue.target_contract_id is not None for issue in issues)
    assert extra_target is None or any(
        issue.target_contract_id == extra_target.id for issue in issues
    )

    monkeypatch.setattr(
        lineage_module, "load_task_view_query_context", lambda _store: query
    )
    data = task_lineage(store, source_id)["data"]
    assert not any(edge["kind"] == "handoff_publication" for edge in data["edges"])
    assert not any(
        issue["code"] == "handoff_publication_ambiguous" for issue in data["issues"]
    )
    if extra_target is not None:
        assert extra_target.id not in data["contract_ids"]
    assert tuple(store.scan_runtime_records()) == before_records
    assert store.get_runtime_revision() == before_revision
    store.close()


def test_handoff_effect_types_mapping_is_not_a_canonical_effect_sequence(
    tmp_path, monkeypatch
):
    from aigineering.plugins.handoff import validate_task_handoff_relations

    store, _source_id, _target_id = _signed_handoff_lineage_fixture(
        tmp_path, "malformed-effect-container"
    )
    before_revision = store.get_runtime_revision()
    before_records = tuple(store.scan_runtime_records())
    query, records, index = _query_with_malformed_handoff_effect_types(store)
    witnesses, issues = validate_task_handoff_relations(query, records, index)
    assert witnesses == ()
    assert len(issues) == 1
    assert issues[0].code == "handoff_publication_unverified"

    import aigineering.task_lineage as lineage_module

    monkeypatch.setattr(
        lineage_module, "load_task_view_query_context", lambda _store: query
    )
    data = task_lineage(store, _source_id)["data"]
    assert not any(edge["kind"] == "handoff_publication" for edge in data["edges"])
    assert any(
        issue["code"] == "handoff_publication_unverified" for issue in data["issues"]
    )
    assert tuple(store.scan_runtime_records()) == before_records
    assert store.get_runtime_revision() == before_revision
    store.close()


@pytest.mark.parametrize(
    "corruption,generic_passes",
    (
        ("missing_terminal", True),
        ("duplicate_terminal", True),
        ("wrong_source_terminal", True),
        ("wrong_kind_terminal", True),
        ("wrong_terminal_state", True),
        ("missing_terminal_parent", True),
        ("target_declaration_direct_parent_missing", True),
        # The link itself has a valid publication proof, but the target member
        # is no longer in that head, so its separate generic proof rejects it.
        ("target_declaration_not_in_head", True),
        ("wrong_effect", True),
        # Generic proof accepts structure without a domain argument; the
        # application owner must bind it to the captured query's domain.
        ("wrong_domain", True),
        # Candidate disagreement is intentionally owned by the generic proof.
        ("wrong_candidate", False),
        ("missing_receipt", False),
        ("duplicate_receipt", False),
        ("missing_head", False),
        ("duplicate_head", False),
    ),
)
def test_handoff_relation_specific_fences_use_coherent_generic_history(
    tmp_path, monkeypatch, corruption, generic_passes
):
    from aigineering.plugins.handoff import validate_task_handoff_relations

    store, source_id, target_id = _signed_handoff_lineage_fixture(
        tmp_path, f"relation-fence-{corruption}"
    )
    before_revision = store.get_runtime_revision()
    before_records = tuple(store.scan_runtime_records())
    query, records, index, link = _query_with_coherent_handoff_corruption(
        store, corruption
    )
    if generic_passes:
        # Wrong domain is checked against the immutable query by the feature
        # owner; all other listed cases use the query domain here.
        proof_domain = None if corruption == "wrong_domain" else query.domain_id
        validate_published_member(
            records,
            link,
            domain_id=proof_domain,
            allow_additional_causal_parents=True,
            record_index=index,
        )
        if corruption == "target_declaration_not_in_head":
            target_declaration = next(
                record
                for record in records
                if record.record_type == "contract.declared"
                and record.payload["contract"]["id"] == target_id
            )
            # The link has a coherent generic proof, but removing its target
            # declaration from that same head invalidates the declaration's
            # own generic publication proof. This is a shared-owner rejection,
            # not evidence that the handoff-specific guard ran.
            with pytest.raises(ValueError):
                validate_published_member(
                    records,
                    target_declaration,
                    domain_id=query.domain_id,
                    record_index=index,
                )
    else:
        with pytest.raises(ValueError):
            validate_published_member(
                records,
                link,
                domain_id=query.domain_id,
                allow_additional_causal_parents=True,
                record_index=index,
            )

    witnesses, issues = validate_task_handoff_relations(query, records, index)
    assert witnesses == ()
    assert len(issues) == 1
    assert issues[0].code == "handoff_publication_unverified"
    assert (issues[0].source_contract_id, issues[0].target_contract_id) == (
        source_id,
        target_id,
    )
    assert "private-wrong-domain-marker" not in repr(issues)
    assert "private-wrong-candidate-marker" not in repr(issues)

    import aigineering.task_lineage as lineage_module

    monkeypatch.setattr(
        lineage_module, "load_task_view_query_context", lambda _store: query
    )
    for endpoint in (source_id, target_id):
        data = task_lineage(store, endpoint)["data"]
        assert not any(edge["kind"] == "handoff_publication" for edge in data["edges"])
        assert set(data["contract_ids"]).issubset({source_id, target_id})
        assert endpoint in data["contract_ids"]
        issue = next(
            issue
            for issue in data["issues"]
            if issue["code"] == "handoff_publication_unverified"
        )
        assert issue["source_contract_id"] == source_id
        assert issue["target_contract_id"] == target_id
        assert "private-wrong-domain-marker" not in repr(data)
        assert "private-wrong-candidate-marker" not in repr(data)
    assert tuple(store.scan_runtime_records()) == before_records
    assert store.get_runtime_revision() == before_revision
    store.close()


@pytest.mark.parametrize("endpoint", ("source", "target"))
def test_duplicate_materialized_handoff_endpoint_is_not_verified(tmp_path, endpoint):
    from aigineering.plugins.handoff import validate_task_handoff_relations

    store, source_id, target_id = _signed_handoff_lineage_fixture(
        tmp_path, f"duplicate-endpoint-{endpoint}"
    )
    before_revision = store.get_runtime_revision()
    before_records = tuple(store.scan_runtime_records())
    query = load_task_view_query_context(store)
    records = tuple(record for _revision, record in query.runtime_records)
    index = index_published_records(records)
    repeated_id = source_id if endpoint == "source" else target_id
    original_contract = next(
        contract for contract in query.contracts if contract.id == repeated_id
    )
    changed_query = replace(
        query,
        contracts=(*query.contracts, original_contract),
    )
    # The publication itself remains generically valid; the captured query has
    # an ambiguous materialized endpoint and the handoff owner emits no edge.
    link = next(record for record in records if record.record_type == "task.handed_off")
    validate_published_member(
        records,
        link,
        domain_id=query.domain_id,
        allow_additional_causal_parents=True,
        record_index=index,
    )
    witnesses, issues = validate_task_handoff_relations(changed_query, records, index)
    assert witnesses == ()
    assert len(issues) == 1
    assert issues[0].code == "handoff_publication_unverified"
    assert set(
        item
        for item in (issues[0].source_contract_id, issues[0].target_contract_id)
        if item is not None
    ).issubset({source_id, target_id})
    assert (
        issues[0].source_contract_id is not None
        or issues[0].target_contract_id is not None
    )
    assert tuple(store.scan_runtime_records()) == before_records
    assert store.get_runtime_revision() == before_revision
    store.close()


def test_unverified_handoff_issue_is_visible_from_both_known_ends_without_expansion(
    tmp_path, monkeypatch
):
    import aigineering.task_lineage as lineage_module

    store, source_id, target_id = _signed_handoff_lineage_fixture(tmp_path, "leaf")
    original_loader = lineage_module.load_task_view_query_context
    query = original_loader(store)
    rows = list(query.runtime_records)
    link_index = next(
        index
        for index, (_revision, record) in enumerate(rows)
        if record.record_type == "task.handed_off"
    )
    revision, link = rows[link_index]
    terminal_id = next(
        record.id
        for _item_revision, record in rows
        if record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == source_id
    )
    rows[link_index] = (
        revision,
        create_runtime_record(
            link.record_type,
            deep_thaw(link.payload),
            causal_parents=tuple(
                parent for parent in link.causal_parents if parent != terminal_id
            ),
            recorded_at=link.recorded_at,
        ),
    )
    monkeypatch.setattr(
        lineage_module,
        "load_task_view_query_context",
        lambda _store: replace(query, runtime_records=tuple(rows)),
    )

    for endpoint in (source_id, target_id):
        data = task_lineage(store, endpoint)["data"]
        assert not any(edge["kind"] == "handoff_publication" for edge in data["edges"])
        issue = next(
            issue
            for issue in data["issues"]
            if issue["code"] == "handoff_publication_unverified"
        )
        assert issue["source_contract_id"] == source_id
        assert issue["target_contract_id"] == target_id
        assert set(data["contract_ids"]).issubset({source_id, target_id})
        assert endpoint in data["contract_ids"]
        assert (target_id if endpoint == source_id else source_id) not in data[
            "contract_ids"
        ]
        assert "source detail must remain private" not in repr(data)
    assert store.get_runtime_revision() == query.revision
    store.close()


def test_lineage_command_is_registered_before_recovery_navigation_work():
    result = CliRunner().invoke(task_group, ["lineage", "task:missing", "--json"])

    assert result.exit_code != 2
    help_result = CliRunner().invoke(
        task_group, ["lineage", "--help"], terminal_width=120
    )
    assert help_result.exit_code == 0
    assert "either endpoint" in help_result.output
    assert "Worker-failure recovery" in help_result.output
    assert "signed retry publications" in " ".join(help_result.output.split())
    assert "handoff publications" in " ".join(help_result.output.split())
    assert "revision publications" in " ".join(help_result.output.split())


class _FailingWorker:
    worker_id = "worker:lineage"

    def invoke(self, contract, disclosed_assets):
        del contract, disclosed_assets
        raise ProviderError(401, "private-provider-detail")


def _signed_failed_recovery(tmp_path, monkeypatch, *, before_failure=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    from aigineering import application

    if os.environ.get("AIG_LINEAGE_USE_NATIVE_REDIS") != "1":
        fixture_redis = MemoryRedis()
        monkeypatch.setattr(
            application, "require_redis", lambda _url=None: fixture_redis
        )
    store = SQLiteStore(tmp_path / "lineage.sqlite")
    signer = Ed25519Signer()
    actor = ActorKey(
        "plugin:recovery.publish.v1",
        "recovery-key",
        signer.kind,
        signer.signer_id,
        (
            "asset.publish",
            "asset.publish.protected",
            "actor.authorize",
            "contract.publish",
            "contract.publish.protected",
            "task.block",
            "worker.register",
        ),
    )
    genesis = create_genesis_manifest("lineage-test", (actor,), "policy:lineage")
    initialize_genesis(store, genesis)
    publisher = CandidatePublisher(store, store, genesis, actor, signer)
    source = build_control_plane_contract(
        name="root task", description="Root source.", outputs=("result",), budget=2
    )
    decision = publisher.publish(
        (contract_declaration_effect(source),), idempotency_key="lineage-source"
    )
    assert decision.accepted
    registry = CandidatePublisherRegistry((("recovery.publish.v1", publisher),))
    host = hosted_worker(
        store,
        _FailingWorker(),
        genesis=genesis,
        authority_key=actor,
        authority_signer=signer,
    )
    if before_failure is not None:
        before_failure(store, source, publisher, host)
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=source.id,
        claim_candidate=host.claim_candidate(
            request_id="lineage-claim", contract_id=source.id
        ),
    )
    assert claimed is not None
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(claimed, host, store, candidate_publishers=registry)
    recoveries = [
        item
        for item in store.get_all_contracts()
        if item.origin == "recovery" and item.name == f"{source.name}.recover"
    ]
    assert len(recoveries) == 1
    return store, source, recoveries[0], publisher, host


def _signed_retry_workspace(
    tmp_path, monkeypatch, *, compound_source=False, source_name="lineage retry source"
):
    import aigineering.application as application
    import aigineering.cli.infrastructure as infrastructure
    import aigineering.cli.retry as retry_cli
    import aigineering.cli.task_board as task_board
    import aigineering.local_publication as local_publication

    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / ".aig" / "store.db"
    key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    fixture_redis = MemoryRedis()
    for module in (application, infrastructure, task_board, local_publication):
        monkeypatch.setattr(
            module, "require_redis", lambda *_args, **_kwargs: fixture_redis
        )
    monkeypatch.setattr(
        local_publication, "sync_runtime", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        task_board,
        "collaboration_service",
        lambda _store: type("Sync", (), {"sync": lambda self: None})(),
    )
    monkeypatch.setattr(task_board, "persistent_store", lambda: SQLiteStore(database))
    monkeypatch.setattr(retry_cli, "_persistent_store", lambda: SQLiteStore(database))

    store = SQLiteStore(database)
    ensure_local_domain(store)
    # P0-7: retry successors must be funded by a containment parent. Publish a
    # funder root and make the retry source its child; the chain of retries
    # (source → first → second) keeps parent_id pointing at the funder.
    funder = publish_task_contract(
        store,
        build_control_plane_contract(
            name=f"{source_name} funder",
            description="Funded containment parent for lineage retry chains.",
            outputs=(),
            budget=12,
        ),
    )
    source = contract_from_fields(
        name=source_name,
        description="Publish exact retry successors through the signed owner.",
        inputs=(),
        outputs=("answer",),
        activation="",
        budget=3,
        tool_scope=(),
        labels=(),
        origin="human",
        parent_id=funder.id,
        minting_authority=(),
    )
    if compound_source:
        import aigineering.local_publication as local_publication

        decision = local_publication.commit_local_effects(
            store,
            (
                contract_declaration_effect(source),
                asset_proposal_effect(
                    Asset(
                        id="lineage-source-context",
                        name="lineage-source-context",
                        content="A source may be published with another signed effect.",
                        created_by=source.id,
                        promptable=True,
                    )
                ),
            ),
            idempotency_key="lineage-compound-source",
        )
        assert decision.accepted
    else:
        source = publish_task_contract(store, source)
    return store, database, source


def _signed_retry_chain(tmp_path, monkeypatch):
    store, database, source = _signed_retry_workspace(tmp_path, monkeypatch)
    first_result, _ = retry_local_task_result(store, source.id)
    first = store.get_contract(first_result["retry_contract_id"])
    second_result, _ = retry_local_task_result(store, first.id)
    second = store.get_contract(second_result["retry_contract_id"])
    return store, database, source, first, second, first_result, second_result


def _query_with_effect_types_mapping(store, member):
    """Return a coherent in-memory publication with Mapping effect_types."""
    query = load_task_view_query_context(store)
    records = tuple(record for _revision, record in query.runtime_records)
    index = index_published_records(records)
    receipt, head = validate_published_member(
        records,
        member,
        domain_id=query.domain_id,
        record_index=index,
    )
    receipt_payload = deep_thaw(receipt.payload)
    receipt_payload["effect_types"] = {
        effect_type: "private-ignored-marker"
        for effect_type in receipt_payload["effect_types"]
    }
    transformed_receipt = create_runtime_record(
        receipt.record_type,
        receipt_payload,
        causal_parents=receipt.causal_parents,
        schema_version=receipt.schema_version,
        recorded_at=receipt.recorded_at,
    )
    id_map = {receipt.id: transformed_receipt.id}
    by_id = {record.id: record for record in records}
    transformed_members = []
    for record_id in head.payload["committed_record_ids"]:
        original = by_id[record_id]
        transformed = create_runtime_record(
            original.record_type,
            deep_thaw(original.payload),
            causal_parents=tuple(
                id_map.get(parent, parent) for parent in original.causal_parents
            ),
            schema_version=original.schema_version,
            recorded_at=original.recorded_at,
        )
        id_map[original.id] = transformed.id
        transformed_members.append(transformed)
    member_ids = [
        id_map[record_id] for record_id in head.payload["committed_record_ids"]
    ]
    transformed_head = create_runtime_record(
        head.record_type,
        {**deep_thaw(head.payload), "committed_record_ids": member_ids},
        causal_parents=(transformed_receipt.id, *member_ids),
        schema_version=head.schema_version,
        recorded_at=head.recorded_at,
    )
    replaced_ids = {
        receipt.id,
        head.id,
        *head.payload["committed_record_ids"],
    }
    rows = tuple(
        (revision, record)
        for revision, record in query.runtime_records
        if record.id not in replaced_ids
    )
    rows += tuple(
        (query.revision, record)
        for record in (transformed_receipt, *transformed_members, transformed_head)
    )
    transformed_query = replace(query, runtime_records=rows)
    transformed_records = tuple(record for _revision, record in rows)
    for record in transformed_records:
        validate_runtime_record(record)
    transformed_index = index_published_records(transformed_records)
    transformed_member = next(
        record
        for record in transformed_members
        if record.record_type == member.record_type
        and record.payload.get("contract", {}).get("id")
        == member.payload.get("contract", {}).get("id")
    )
    validate_published_member(
        transformed_records,
        transformed_member,
        domain_id=query.domain_id,
        record_index=transformed_index,
    )
    return transformed_query, transformed_records, transformed_index, transformed_member


def test_retry_mapping_effect_types_cannot_create_a_lineage_edge(tmp_path, monkeypatch):
    import aigineering.task_publication as publication_module

    store, _database, source, first, _second, _first_result, _second_result = (
        _signed_retry_chain(tmp_path, monkeypatch)
    )
    try:
        baseline = load_task_view_query_context(store)
        baseline_records = tuple(row for _revision, row in baseline.runtime_records)
        baseline_index = index_published_records(baseline_records)
        baseline_witnesses, _ = publication_module.validate_retry_publication_relations(
            baseline,
            baseline_records,
            baseline_index,
        )
        assert any(w.target_contract_id == first.id for w in baseline_witnesses)

        declaration = next(
            row
            for row in baseline_records
            if row.record_type == "contract.declared"
            and row.payload.get("contract", {}).get("id") == first.id
        )
        changed_query, records, index, changed_declaration = (
            _query_with_effect_types_mapping(store, declaration)
        )
        # Generic receipt/head/member integrity is still valid; the feature
        # reader must independently reject the malformed effect container.
        validate_published_member(
            records,
            changed_declaration,
            domain_id=changed_query.domain_id,
            record_index=index,
        )
        witnesses, issues = publication_module.validate_retry_publication_relations(
            changed_query,
            records,
            index,
        )
        assert not any(w.target_contract_id == first.id for w in witnesses)
        assert any(i.target_contract_id == first.id for i in issues)
        assert "private-ignored-marker" not in repr(issues)
    finally:
        store.close()


def test_recovery_mapping_effect_types_cannot_create_a_lineage_edge(
    tmp_path, monkeypatch
):
    import aigineering.task_lineage as lineage_module

    store, source, recovery, _publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        before_records = tuple(store.scan_runtime_records())
        before_revision = store.get_runtime_revision()
        baseline = task_lineage(store, source.id)
        assert any(
            edge["kind"] == "recovery_publication" for edge in baseline["data"]["edges"]
        )
        declaration = next(
            record
            for _revision, record in before_records
            if record.record_type == "contract.declared"
            and record.payload.get("contract", {}).get("id") == recovery.id
        )
        changed_query, records, index, changed_declaration = (
            _query_with_effect_types_mapping(store, declaration)
        )
        validate_published_member(
            records,
            changed_declaration,
            domain_id=changed_query.domain_id,
            record_index=index,
        )
        monkeypatch.setattr(
            lineage_module, "load_task_view_query_context", lambda _store: changed_query
        )
        result = task_lineage(store, source.id)
        assert not any(
            edge["kind"] == "recovery_publication" for edge in result["data"]["edges"]
        )
        assert any(
            issue["code"] == "recovery_publication_unverified"
            for issue in result["data"]["issues"]
        )
        assert "private-ignored-marker" not in json.dumps(result)
        assert tuple(store.scan_runtime_records()) == before_records
        assert store.get_runtime_revision() == before_revision
    finally:
        store.close()


def test_root_null_recovery_is_proved_in_both_directions_and_keeps_own_status(
    tmp_path, monkeypatch
):
    store, source, recovery, publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        descendant = contract_from_fields(
            name="recovery follow-up",
            description="An ordinary descendant stays a separate task.",
            outputs=("follow-up",),
            budget=1,
            parent_id=recovery.id,
            inputs=(),
            activation="",
            tool_scope=(),
            labels=(),
            origin="worker",
        )
        child_decision = publisher.publish(
            (contract_declaration_effect(descendant),),
            idempotency_key="lineage-descendant",
        )
        assert child_decision.accepted
        from_source = task_lineage(store, source.id)
        from_recovery = task_lineage(store, recovery.id)
        assert (
            from_source["data"]["contract_ids"] == from_recovery["data"]["contract_ids"]
        )
        assert {row["contract_id"] for row in from_source["data"]["contracts"]} == {
            source.id,
            recovery.id,
            descendant.id,
        }
        edges = from_source["data"]["edges"]
        recovery_edges = [
            edge for edge in edges if edge["kind"] == "recovery_publication"
        ]
        assert len(recovery_edges) == 1
        assert recovery_edges[0]["source_contract_id"] == source.id
        assert recovery_edges[0]["target_contract_id"] == recovery.id
        assert recovery_edges[0]["receipt_id"]
        assert recovery_edges[0]["head_id"]
        recovery_declaration = next(
            record
            for _revision, record in store.scan_runtime_records()
            if record.id == recovery_edges[0]["declaration_id"]
        )
        records = tuple(record for _revision, record in store.scan_runtime_records())
        from aigineering.core.publication_history import (
            validate_published_member as shared_validate_published_member,
        )

        assert validate_published_member is shared_validate_published_member
        indexed_receipt, indexed_head = validate_published_member(
            records,
            recovery_declaration,
            domain_id=from_source["data"]["domain_id"],
            record_index=index_published_records(records),
        )
        default_receipt, default_head = validate_published_member(
            records, recovery_declaration, domain_id=from_source["data"]["domain_id"]
        )
        assert indexed_receipt.id == recovery_edges[0]["receipt_id"]
        assert indexed_head.id == recovery_edges[0]["head_id"]
        assert (indexed_receipt.id, indexed_head.id) == (
            default_receipt.id,
            default_head.id,
        )
        detached_records = tuple(replace(record) for record in records)
        with pytest.raises(ValueError, match="does not match captured record history"):
            validate_published_member(
                detached_records,
                recovery_declaration,
                domain_id=from_source["data"]["domain_id"],
                record_index=index_published_records(records),
            )
        assert any(
            edge["kind"] == "containment"
            and edge["source_contract_id"] == recovery.id
            and edge["target_contract_id"] == descendant.id
            for edge in edges
        )
        containment_edge = next(
            edge
            for edge in edges
            if edge["kind"] == "containment"
            and edge["target_contract_id"] == descendant.id
        )
        assert containment_edge["evidence_record_ids"]
        assert containment_edge["receipt_id"]
        assert containment_edge["head_id"]
        assert from_source["data"]["covered_relations"] == [
            "containment",
            "recovery_publication",
            "retry_publication",
            "handoff_publication",
            "revision_publication",
        ]
        cards = {
            row["contract_id"]: row["task"] for row in from_source["data"]["contracts"]
        }
        assert cards[source.id]["status"] == "failed"
        assert cards[recovery.id]["status"] in {"ready", "blocked"}
        assert from_source["data"]["source_diagnostics"]
        assert "private-provider-detail" not in json.dumps(from_source)

        # A correctly signed recovery-origin Contract is not a verified edge
        # without the recovery publisher's exact source causal parent.
        decoy = contract_from_fields(
            name="decoy.recover",
            description=json.dumps({"failed_contract_id": source.id}),
            origin="recovery",
            parent_id=None,
            inputs=(f"_fail_context_{source.id}",),
            outputs=("result",),
            budget=1,
            activation="",
            tool_scope=(),
            labels=(),
        )
        decoy_decision = publisher.publish(
            (contract_declaration_effect(decoy),),
            idempotency_key="lineage-decoy-no-source",
        )
        assert decoy_decision.accepted
        decoy_result = task_lineage(store, source.id)
        assert decoy.id not in decoy_result["data"]["contract_ids"]
    finally:
        store.close()


def test_retry_publication_chain_is_bidirectional_exact_and_read_only(
    tmp_path, monkeypatch
):
    store, database, source, first, second, first_result, second_result = (
        _signed_retry_chain(tmp_path, monkeypatch)
    )
    try:
        before = tuple(store.scan_runtime_records())
        before_revision = store.get_runtime_revision()
        receipt = next(
            record
            for _revision, record in before
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id") == first_result["candidate_id"]
        )
        assert "idempotency_key" not in receipt.payload

        results = [
            task_lineage(store, contract.id)["data"]
            for contract in (source, first, second)
        ]
        assert all(
            result["contract_ids"]
            == sorted({source.parent_id, source.id, first.id, second.id})
            for result in results
        )
        retry_edges = [
            edge for edge in results[0]["edges"] if edge["kind"] == "retry_publication"
        ]
        assert len(retry_edges) == 2
        assert {
            (edge["source_contract_id"], edge["target_contract_id"])
            for edge in retry_edges
        } == {(source.id, first.id), (first.id, second.id)}
        for result in results[1:]:
            assert [
                edge for edge in result["edges"] if edge["kind"] == "retry_publication"
            ] == retry_edges
        assert "retry_publication" in results[0]["covered_relations"]
        assert all(
            edge[field]
            for edge in retry_edges
            for field in (
                "source_declaration_id",
                "source_receipt_id",
                "source_head_id",
                "target_declaration_id",
                "target_receipt_id",
                "target_head_id",
                "candidate_id",
            )
        )
        cards = {row["contract_id"]: row["task"] for row in results[0]["contracts"]}
        assert all(
            cards[contract.id]["status"] in {"ready", "blocked"}
            for contract in (source, first, second)
        )

        import aigineering.task_lineage as lineage_module

        original_load = lineage_module.load_task_view_query_context
        original_index = lineage_module.index_published_records
        counts = {"query": 0, "index": 0}

        def counted_load(value):
            counts["query"] += 1
            return original_load(value)

        def counted_index(value):
            counts["index"] += 1
            return original_index(value)

        monkeypatch.setattr(
            lineage_module, "load_task_view_query_context", counted_load
        )
        monkeypatch.setattr(lineage_module, "index_published_records", counted_index)
        task_lineage(store, source.id)
        assert counts == {"query": 1, "index": 1}
        monkeypatch.setattr(
            lineage_module, "load_task_view_query_context", original_load
        )
        monkeypatch.setattr(lineage_module, "index_published_records", original_index)

        repeated_first, _ = retry_local_task_result(store, source.id)
        repeated_second, _ = retry_local_task_result(store, first.id)
        assert repeated_first == first_result
        assert repeated_second == second_result
        assert store.get_runtime_revision() == before_revision
        assert tuple(store.scan_runtime_records()) == before

        class Collaboration:
            def sync(self):
                return None

        monkeypatch.setattr(
            lineage_cli, "_persistent_snapshot", lambda: SQLiteStore(database)
        )
        monkeypatch.setattr(
            lineage_cli, "collaboration_service", lambda _store: Collaboration()
        )
        for endpoint in (source, second):
            rendered = CliRunner().invoke(
                task_group, ["lineage", endpoint.id, "--json"]
            )
            assert rendered.exit_code == 0, rendered.output
            assert json.loads(rendered.output)["data"]["contract_ids"] == sorted(
                {source.parent_id, source.id, first.id, second.id}
            )
        human = CliRunner().invoke(task_group, ["lineage", source.id])
        assert human.exit_code == 0, human.output
        for edge in retry_edges:
            for field in (
                "source_declaration_id",
                "source_receipt_id",
                "source_head_id",
                "target_declaration_id",
                "target_receipt_id",
                "target_head_id",
            ):
                assert edge[field] in human.output

        reopened = SQLiteStore(database)
        try:
            reopened_data = task_lineage(reopened, second.id)["data"]
            assert reopened_data["edges"] == results[0]["edges"]
            assert tuple(reopened.scan_runtime_records()) == before
        finally:
            reopened.close()
        copied = store.snapshot()
        try:
            copied_data = task_lineage(copied, first.id)["data"]
            assert copied_data["contract_ids"] == results[0]["contract_ids"]
            assert copied_data["edges"] == results[0]["edges"]
        finally:
            copied.close()
    finally:
        store.close()


def test_compound_published_source_can_have_an_exact_retry_successor(
    tmp_path, monkeypatch
):
    store, _database, source = _signed_retry_workspace(
        tmp_path, monkeypatch, compound_source=True
    )
    try:
        retry_result, _revision = retry_local_task_result(store, source.id)
        target = store.get_contract(retry_result["retry_contract_id"])
        source_receipt = next(
            record
            for _revision, record in store.scan_runtime_records()
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id")
            == next(
                declaration.payload["candidate_id"]
                for _row_revision, declaration in store.scan_runtime_records()
                if declaration.record_type == "contract.declared"
                and declaration.payload.get("contract", {}).get("id") == source.id
            )
        )
        assert set(source_receipt.payload["effect_types"]) == {
            "contract.declare",
            "asset.propose",
        }
        result = task_lineage(store, source.id)["data"]
        assert result["contract_ids"] == sorted(
            {source.parent_id, source.id, target.id}
        )
        edge = next(
            item for item in result["edges"] if item["kind"] == "retry_publication"
        )
        assert (edge["source_contract_id"], edge["target_contract_id"]) == (
            source.id,
            target.id,
        )
    finally:
        store.close()


def test_retry_target_with_a_compound_signed_publication_is_not_verified(
    tmp_path, monkeypatch
):
    import aigineering.local_publication as local_publication

    store, _database, source = _signed_retry_workspace(tmp_path, monkeypatch)
    try:
        from aigineering.plugins.task_semantics import retry_contract

        target = retry_contract(source)
        decision = local_publication.commit_local_effects(
            store,
            (
                contract_declaration_effect(target),
                asset_proposal_effect(
                    Asset(
                        id="lineage-retry-extra-effect",
                        name="lineage-retry-extra-effect",
                        content="An extra signed effect invalidates retry target shape.",
                        created_by=source.id,
                        promptable=True,
                    )
                ),
            ),
            idempotency_key="lineage-compound-retry-target",
            causal_parents=(source.id,),
        )
        assert decision.accepted
        result = task_lineage(store, source.id)["data"]
        # The compound target is a published child of the shared funder, so it
        # joins the component via containment; only its retry edge is unverified.
        assert result["contract_ids"] == sorted(
            {source.parent_id, source.id, target.id}
        )
        assert not any(edge["kind"] == "retry_publication" for edge in result["edges"])
        assert any(
            issue["code"] == "retry_publication_unverified"
            and issue.get("source_contract_id") == source.id
            and issue.get("target_contract_id") == target.id
            for issue in result["issues"]
        )
    finally:
        store.close()


def test_compound_retry_cached_as_source_cannot_bypass_target_effect_check(
    tmp_path, monkeypatch
):
    import aigineering.local_publication as local_publication

    store, _database, source = _signed_retry_workspace(
        tmp_path, monkeypatch, source_name="lineage retry order 2"
    )
    try:
        from aigineering.plugins.task_semantics import retry_contract

        compound_target = retry_contract(source)
        decision = local_publication.commit_local_effects(
            store,
            (
                contract_declaration_effect(compound_target),
                asset_proposal_effect(
                    Asset(
                        id="lineage-ordering-extra-effect",
                        name="lineage-ordering-extra-effect",
                        content="This compound retry remains only an exact source.",
                        created_by=source.id,
                        promptable=True,
                    )
                ),
            ),
            idempotency_key="lineage-ordering-compound-retry",
            causal_parents=(source.id,),
        )
        assert decision.accepted
        normal_result, _revision = retry_local_task_result(store, compound_target.id)
        final_target = store.get_contract(normal_result["retry_contract_id"])
        assert final_target.id < compound_target.id

        from_source = task_lineage(store, source.id)["data"]
        assert from_source["contract_ids"] == sorted(
            {
                source.parent_id,
                source.id,
                compound_target.id,
                final_target.id,
            }
        )
        retry_edges = [
            edge for edge in from_source["edges"] if edge["kind"] == "retry_publication"
        ]
        assert len(retry_edges) == 1
        assert retry_edges[0]["source_contract_id"] == compound_target.id
        assert retry_edges[0]["target_contract_id"] == final_target.id
        assert any(
            issue.get("source_contract_id") == source.id
            and issue.get("target_contract_id") == compound_target.id
            and issue["code"] == "retry_publication_unverified"
            for issue in from_source["issues"]
        )

        from_compound_source = task_lineage(store, compound_target.id)["data"]
        assert from_compound_source["contract_ids"] == sorted(
            {
                compound_target.parent_id,
                source.id,
                compound_target.id,
                final_target.id,
            }
        )
        assert [
            edge
            for edge in from_compound_source["edges"]
            if edge["kind"] == "retry_publication"
        ]
    finally:
        store.close()


def test_handoff_publication_is_a_valid_source_but_retry_remains_unaccepted(
    tmp_path,
    monkeypatch,
):
    from test_task_handoff import _publish_handoff, _workspace

    from aigineering.core.candidate_publisher import publish_effect
    from aigineering.plugins.handoff import project_task_handoffs
    from aigineering.plugins.task_semantics import retry_contract

    store, signer, actor, genesis, original = _workspace(tmp_path)
    try:
        handoff = _publish_handoff(
            store, signer, actor, genesis, original, "lineage-handoff-source"
        )
        assert handoff.accepted
        link = project_task_handoffs(
            (record for _revision, record in store.scan_runtime_records()),
            source_contract_id=original.id,
        )[0]
        source = store.get_contract(link.payload["successor_id"])
        retry_target = retry_contract(source)
        records = tuple(record for _revision, record in store.scan_runtime_records())
        source_declaration = next(
            record
            for record in records
            if record.record_type == "contract.declared"
            and record.payload.get("contract", {}).get("id") == source.id
        )
        source_receipt, _source_head = validate_published_member(
            records,
            source_declaration,
            domain_id=genesis.id,
            record_index=index_published_records(records),
        )
        assert tuple(source_receipt.payload["effect_types"]) == ("task.handoff",)
        retry = publish_effect(
            store,
            store,
            genesis,
            actor,
            signer,
            contract_declaration_effect(retry_target),
            idempotency_key=f"lineage-retry:{source.id}",
            causal_parents=(source.id,),
        )
        assert not retry.accepted
        assert retry.trace_entries
        assert retry.trace_entries[-1].budget_remaining == 0
        rejection_receipts = tuple(
            record
            for record in retry.runtime_records
            if record.record_type == "candidate.received"
        )
        assert len(rejection_receipts) == 1
        assert store.get_runtime_record(rejection_receipts[0].id) is not None
        assert not any(
            record.record_type == "contract.declared"
            and record.payload.get("contract", {}).get("id") == retry_target.id
            for _revision, record in store.scan_runtime_records()
        )
        before_lineage = tuple(store.scan_runtime_records())
        result = task_lineage(store, source.id)["data"]
        assert source.id in result["contract_ids"]
        assert retry_target.id not in result["contract_ids"]
        assert not any(edge["kind"] == "retry_publication" for edge in result["edges"])
        rejection_issue = next(
            issue
            for issue in result["issues"]
            if issue["code"] == "declaration_candidate_rejected_unverified"
        )
        assert rejection_issue["source_contract_id"] == source.id
        assert "target_contract_id" not in rejection_issue
        assert retry_target.id not in json.dumps(result)
        assert tuple(store.scan_runtime_records()) == before_lineage

        import aigineering.task_publication as publication_module

        query = load_task_view_query_context(store)
        rows = tuple(record for _revision, record in query.runtime_records)
        rejected = next(
            record
            for record in rows
            if record.record_type == "candidate.rejected"
            and record.payload.get("candidate_id") == retry.candidate_id
        )
        receipt = next(
            record
            for record in rows
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id") == retry.candidate_id
        )
        payload = deep_thaw(receipt.payload)
        payload["effect_types"] = {"contract.declare": "private-effect-marker"}
        malformed_receipt = create_runtime_record(
            receipt.record_type,
            payload,
            causal_parents=receipt.causal_parents,
            schema_version=receipt.schema_version,
            recorded_at=receipt.recorded_at,
        )
        malformed_rejection = create_runtime_record(
            rejected.record_type,
            deep_thaw(rejected.payload),
            causal_parents=(malformed_receipt.id,),
            schema_version=rejected.schema_version,
            recorded_at=rejected.recorded_at,
        )
        replacement = {receipt.id: malformed_receipt, rejected.id: malformed_rejection}
        transformed = tuple(replacement.get(record.id, record) for record in rows)
        changed_query = replace(
            query,
            runtime_records=tuple(
                (revision, replacement.get(record.id, record))
                for revision, record in query.runtime_records
            ),
        )
        index = index_published_records(transformed)
        validator = publication_module.validate_retry_publication_relations
        witnesses, issues = validator(changed_query, transformed, index)
        assert not witnesses
        assert not any(
            issue.code == "declaration_candidate_rejected_unverified"
            for issue in issues
        )
        assert "private-effect-marker" not in repr(issues)
        assert tuple(store.scan_runtime_records()) == before_lineage
    finally:
        store.close()


@pytest.mark.parametrize(
    "corruption", ["missing_head", "duplicate_head", "unknown_source"]
)
def test_retry_head_integrity_issue_is_visible_from_known_source(
    tmp_path, monkeypatch, corruption
):
    store, _database, source, target, *_ = _signed_retry_chain(tmp_path, monkeypatch)
    try:
        original = tuple(store.scan_runtime_records())
        target_declaration = next(
            record
            for _revision, record in original
            if record.record_type == "contract.declared"
            and record.payload.get("contract", {}).get("id") == target.id
        )
        receipt_id = target_declaration.causal_parents[0]
        receipt = next(
            record for _revision, record in original if record.id == receipt_id
        )
        candidate_id = receipt.payload["candidate_id"]
        head_pair = next(
            pair
            for pair in original
            if pair[1].record_type == "candidate.committed"
            and pair[1].payload.get("candidate_id") == candidate_id
        )
        if corruption == "unknown_source":
            payload = deep_thaw(receipt.payload)
            changed_receipt = create_runtime_record(
                receipt.record_type,
                payload,
                causal_parents=("private-unmaterialized-source-marker",),
                schema_version=receipt.schema_version,
                recorded_at=receipt.recorded_at,
            )
            changed = tuple(
                (revision, changed_receipt if record is receipt else record)
                for revision, record in original
            )
        elif corruption == "missing_head":
            changed = tuple(pair for pair in original if pair is not head_pair)
        else:
            changed = (*original, head_pair)
        monkeypatch.setattr(store, "scan_runtime_records", lambda: changed)
        selected_id = target.id if corruption == "unknown_source" else source.id
        result = task_lineage(store, selected_id)["data"]
        if corruption == "unknown_source":
            assert result["contract_ids"] == [target.id]
        else:
            assert source.id in result["contract_ids"]
            assert target.id not in result["contract_ids"]
        matching_issue = next(
            issue
            for issue in result["issues"]
            if issue.get("target_contract_id") == target.id
            and issue["code"] == "retry_publication_unverified"
        )
        if corruption == "unknown_source":
            assert "source_contract_id" not in matching_issue
            assert "private-unmaterialized-source-marker" not in json.dumps(result)
        else:
            assert matching_issue.get("source_contract_id") == source.id
        if corruption != "unknown_source":
            assert any(
                issue.get("source_contract_id") == source.id
                and issue.get("target_contract_id") == target.id
                and issue["code"] == "retry_publication_unverified"
                for issue in result["issues"]
            )
        assert not any(edge["kind"] == "retry_publication" for edge in result["edges"])
    finally:
        store.close()


@pytest.mark.parametrize(
    ("corruption", "expected_source"),
    [
        ("wrong_effect", True),
        ("wrong_domain", False),
        ("missing_parent", False),
        ("multiple_parents", False),
        ("missing_receipt", False),
        ("duplicate_receipt", False),
        ("missing_declaration", False),
        ("duplicate_declaration", False),
        ("mismatched_materialization", True),
    ],
)
def test_retry_specific_malformed_publications_are_safe_from_both_known_ends(
    tmp_path, monkeypatch, corruption, expected_source
):
    store, _database, source, target, *_ = _signed_retry_chain(tmp_path, monkeypatch)
    try:
        original = tuple(store.scan_runtime_records())
        declaration = next(
            record
            for _revision, record in original
            if record.record_type == "contract.declared"
            and record.payload.get("contract", {}).get("id") == target.id
        )
        receipt = next(
            record
            for _revision, record in original
            if record.id == declaration.causal_parents[0]
        )
        changed = list(original)
        if corruption in {
            "wrong_effect",
            "wrong_domain",
            "missing_parent",
            "multiple_parents",
        }:
            payload = deep_thaw(receipt.payload)
            if corruption == "wrong_effect":
                payload["effect_types"] = ["asset.propose"]
            elif corruption == "wrong_domain":
                payload["domain_id"] = "private-wrong-domain-marker"
            if corruption == "missing_parent":
                parents = ()
            elif corruption == "multiple_parents":
                parents = (*receipt.causal_parents, "private-extra-parent-marker")
            else:
                parents = receipt.causal_parents
            changed_receipt = create_runtime_record(
                receipt.record_type,
                payload,
                causal_parents=parents,
                schema_version=receipt.schema_version,
                recorded_at=receipt.recorded_at,
            )
            changed_declaration_payload = deep_thaw(declaration.payload)
            changed_declaration = create_runtime_record(
                declaration.record_type,
                changed_declaration_payload,
                causal_parents=(changed_receipt.id,),
                schema_version=declaration.schema_version,
                recorded_at=declaration.recorded_at,
            )
            changed = [
                (revision, changed_receipt if record is receipt else record)
                for revision, record in changed
            ]
            changed = [
                (revision, changed_declaration if record is declaration else record)
                for revision, record in changed
            ]
        elif corruption == "missing_receipt":
            changed_declaration = create_runtime_record(
                declaration.record_type,
                deep_thaw(declaration.payload),
                causal_parents=("private-missing-receipt-marker",),
                schema_version=declaration.schema_version,
                recorded_at=declaration.recorded_at,
            )
            changed = [
                (revision, changed_declaration if record is declaration else record)
                for revision, record in changed
            ]
        elif corruption == "duplicate_receipt":
            changed.append((original[-1][0], receipt))
        elif corruption == "missing_declaration":
            changed = [pair for pair in changed if pair[1] is not declaration]
        elif corruption == "duplicate_declaration":
            duplicate = create_runtime_record(
                declaration.record_type,
                deep_thaw(declaration.payload),
                causal_parents=declaration.causal_parents,
                schema_version=declaration.schema_version,
                recorded_at=declaration.recorded_at,
            )
            changed.append((original[-1][0], duplicate))
        else:
            payload = deep_thaw(declaration.payload)
            payload["contract"]["description"] += " altered"
            changed_declaration = create_runtime_record(
                declaration.record_type,
                payload,
                causal_parents=declaration.causal_parents,
                schema_version=declaration.schema_version,
                recorded_at=declaration.recorded_at,
            )
            changed = [
                (revision, changed_declaration if record is declaration else record)
                for revision, record in changed
            ]

        frozen = tuple(changed)
        before_revision = store.get_runtime_revision()
        persisted_scan = store.scan_runtime_records
        before_persisted = tuple(persisted_scan())
        monkeypatch.setattr(store, "scan_runtime_records", lambda: frozen)
        source_view = task_lineage(store, source.id)["data"]
        target_view = task_lineage(store, target.id)["data"]
        assert not any(
            edge["kind"] == "retry_publication" for edge in source_view["edges"]
        )
        assert not any(
            edge["kind"] == "retry_publication" for edge in target_view["edges"]
        )
        assert source.id in source_view["contract_ids"]
        assert target.id not in source_view["contract_ids"]
        assert target_view["contract_ids"] == [target.id]
        target_issue = next(
            issue
            for issue in target_view["issues"]
            if issue.get("target_contract_id") == target.id
            and issue["code"] == "retry_publication_unverified"
        )
        if expected_source:
            source_issue = next(
                issue
                for issue in source_view["issues"]
                if issue.get("target_contract_id") == target.id
                and issue["code"] == "retry_publication_unverified"
            )
            assert source_issue.get("source_contract_id") == source.id
            assert target_issue.get("source_contract_id") == source.id
        else:
            assert "source_contract_id" not in target_issue
            # P0-7: source and target share the containment funder, so the target
            # may only surface in the source view through the unverified sibling
            # issue's parent_contract_id reference — never as a component member,
            # edge endpoint, card, or diagnostic.
        leak_surface = json.dumps(
            {
                "contract_ids": source_view["contract_ids"],
                "contracts": source_view["contracts"],
                "edges": source_view["edges"],
                "source_diagnostics": source_view["source_diagnostics"],
            }
        )
        assert target.id not in leak_surface
        assert all(
            issue.get("parent_contract_id") == source.parent_id
            and issue.get("contract_id") == target.id
            for issue in source_view["issues"]
            if issue.get("contract_id") == target.id
        )
        rendered = json.dumps({"source": source_view, "target": target_view})
        assert "private-" not in rendered
        assert store.get_runtime_revision() == before_revision
        assert tuple(persisted_scan()) == before_persisted
    finally:
        store.close()


@pytest.mark.parametrize(
    "mismatch", ["detached_record", "reordered_records", "foreign_index"]
)
def test_retry_relation_validator_rejects_detached_or_reordered_snapshot_inputs(
    tmp_path, monkeypatch, mismatch
):
    from aigineering.task_publication import validate_retry_publication_relations

    store, _database, source, first, second, *_ = _signed_retry_chain(
        tmp_path, monkeypatch
    )
    try:
        context = load_task_view_query_context(store)
        records = tuple(record for _revision, record in context.runtime_records)
        index = index_published_records(records)
        witnesses, _issues = validate_retry_publication_relations(
            context, records, index
        )
        assert len(witnesses) == 2
        if mismatch == "detached_record":
            supplied = (replace(records[0]), *records[1:])
            supplied_index = index
        elif mismatch == "reordered_records":
            supplied = tuple(reversed(records))
            supplied_index = index
        else:
            supplied = records
            foreign = (*records, create_runtime_record("test.unrelated", {}))
            supplied_index = index_published_records(foreign)
            foreign_witnesses, foreign_issues = validate_retry_publication_relations(
                context, records, supplied_index
            )
            assert foreign_witnesses == ()
            assert {
                (issue.source_contract_id, issue.target_contract_id)
                for issue in foreign_issues
                if issue.code == "retry_publication_unverified"
            } == {(source.id, first.id), (first.id, second.id)}
        if mismatch == "foreign_index":
            target_declaration = next(
                record
                for record in records
                if record.record_type == "contract.declared"
                and record.payload.get("contract", {}).get("origin") == "retry"
            )
            with pytest.raises(ValueError, match="publication index"):
                validate_published_member(
                    records,
                    target_declaration,
                    domain_id=context.domain_id,
                    record_index=supplied_index,
                )
        else:
            with pytest.raises(ValueError, match="captured query"):
                validate_retry_publication_relations(context, supplied, supplied_index)
        assert store.get_runtime_revision() == context.revision
    finally:
        store.close()


def test_lineage_cli_renders_json_and_human_without_changing_store(
    tmp_path, monkeypatch
):
    store, source, recovery, _publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    before = tuple(store.scan_runtime_records())

    class Collaboration:
        def sync(self):
            return None

    monkeypatch.setattr(
        lineage_cli,
        "_persistent_snapshot",
        lambda: store.snapshot(),
    )
    monkeypatch.setattr(
        lineage_cli, "collaboration_service", lambda _store: Collaboration()
    )
    lineage_group = click.Group(name="task")
    lineage_cli.register_task_lineage_command(lineage_group)
    json_result = CliRunner().invoke(lineage_group, ["lineage", source.id, "--json"])
    assert json_result.exit_code == 0, json_result.output
    envelope = json.loads(json_result.output)
    assert envelope["ok"] is True
    assert envelope["data"]["selected_contract_id"] == source.id
    assert recovery.id in envelope["data"]["contract_ids"]
    human_result = CliRunner().invoke(lineage_group, ["lineage", recovery.id])
    assert human_result.exit_code == 0, human_result.output
    assert source.id in human_result.output
    assert recovery.id in human_result.output
    assert "receipt=" in human_result.output
    assert "declaration=" in human_result.output
    assert "head=" in human_result.output
    assert "verified source recovery schedule:" in human_result.output
    reopened = SQLiteStore(tmp_path / "lineage.sqlite")
    try:
        assert tuple(reopened.scan_runtime_records()) == before
        reopened_lineage = task_lineage(reopened, source.id)["data"]
        assert recovery.id in reopened_lineage["contract_ids"]
        assert any(
            edge["kind"] == "recovery_publication" for edge in reopened_lineage["edges"]
        )
        from aigineering.core.integrity import check_store_integrity

        assert check_store_integrity(reopened)["consistent"] is True
        assert tuple(reopened.scan_runtime_records()) == before
    finally:
        reopened.close()
    copied = store.snapshot()
    try:
        copied_lineage = task_lineage(copied, recovery.id)["data"]
        assert copied_lineage["contract_ids"] == reopened_lineage["contract_ids"]
        assert copied_lineage["edges"] == reopened_lineage["edges"]
        assert check_store_integrity(copied)["consistent"] is True
    finally:
        copied.close()
    store.close()


def test_invocation_binding_rejects_a_query_from_another_store(tmp_path, monkeypatch):
    from aigineering.task_diagnostics import task_runs
    from aigineering.task_view_query import TaskViewQueryMismatch

    first, source, _recovery, _publisher, _host = _signed_failed_recovery(
        tmp_path / "one", monkeypatch
    )
    second, _other_source, _other_recovery, _other_publisher, _other_host = (
        _signed_failed_recovery(tmp_path / "two", monkeypatch)
    )
    try:
        query = load_task_view_query_context(first)
        grant = next(
            record
            for _revision, record in query.runtime_records
            if record.record_type == "claim.granted"
            and record.payload.get("contract_id") == source.id
        )
        with pytest.raises(TaskViewQueryMismatch):
            task_invocation_failure_binding(
                second,
                source.id,
                grant.payload["claim_id"],
                grant.payload["package_id"],
                grant.payload["worker_id"],
                grant.payload["epoch"],
                query_context=query,
            )
        row = task_runs(first, source.id, limit=100, query_context=query)["data"][
            "runs"
        ][0]
        assert row["status"] == "invocation_failed"
    finally:
        first.close()
        second.close()


def test_wrong_recovery_publisher_cannot_expand_component(tmp_path, monkeypatch):
    store, source, recovery, _publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        original = tuple(store.scan_runtime_records())
        receipt_pair = next(
            pair
            for pair in original
            if pair[1].record_type == "candidate.received"
            and pair[1].payload.get("actor_id") == "plugin:recovery.publish.v1"
        )
        revision, receipt = receipt_pair
        payload = deep_thaw(receipt.payload)
        payload["actor_id"] = "plugin:unrelated.publisher"
        changed = create_runtime_record(
            receipt.record_type,
            payload,
            causal_parents=receipt.causal_parents,
            schema_version=receipt.schema_version,
            recorded_at=receipt.recorded_at,
        )
        corrupted = tuple(
            (row_revision, changed if row is receipt else row)
            for row_revision, row in original
        )
        monkeypatch.setattr(store, "scan_runtime_records", lambda: corrupted)
        result = task_lineage(store, recovery.id)
        assert result["data"]["contract_ids"] == [recovery.id]
        assert not any(
            edge["kind"] == "recovery_publication" for edge in result["data"]["edges"]
        )
        assert result["data"]["revision"] == store.get_runtime_revision()
        assert revision > 0
        assert source.id not in result["data"]["contract_ids"]
    finally:
        store.close()


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "malformed"])
def test_recovery_declaration_integrity_issues_are_visible(
    tmp_path, monkeypatch, corruption
):
    store, _source, recovery, _publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        original = tuple(store.scan_runtime_records())
        declaration_pair = next(
            pair
            for pair in original
            if pair[1].record_type == "contract.declared"
            and pair[1].payload.get("contract", {}).get("id") == recovery.id
        )
        if corruption == "missing":
            changed = tuple(pair for pair in original if pair != declaration_pair)
        elif corruption == "duplicate":
            changed = (*original, declaration_pair)
        else:
            revision, declaration = declaration_pair
            payload = deep_thaw(declaration.payload)
            payload["contract"] = None
            malformed = create_runtime_record(
                declaration.record_type,
                payload,
                causal_parents=declaration.causal_parents,
                schema_version=declaration.schema_version,
                recorded_at=declaration.recorded_at,
            )
            changed = tuple(
                (row_revision, malformed if record is declaration else record)
                for row_revision, record in original
            )
            assert revision > 0
        monkeypatch.setattr(store, "scan_runtime_records", lambda: changed)
        result = task_lineage(store, recovery.id)
        assert result["data"]["contract_ids"] == [recovery.id]
        assert any(
            issue.get("contract_id") == recovery.id
            and issue["code"]
            in {
                "contract_publication_unverified",
                "recovery_publication_unverified",
            }
            for issue in result["data"]["issues"]
        )
        assert not result["data"]["edges"]
    finally:
        store.close()


def test_materialized_parent_is_not_a_containment_witness(tmp_path, monkeypatch):
    store, _source, recovery, publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        descendant = contract_from_fields(
            name="signed descendant",
            description="Its materialized parent is checked against the declaration.",
            outputs=("follow-up",),
            budget=1,
            parent_id=recovery.id,
            inputs=(),
            activation="",
            tool_scope=(),
            labels=(),
            origin="worker",
        )
        decision = publisher.publish(
            (contract_declaration_effect(descendant),),
            idempotency_key="lineage-signed-descendant",
        )
        assert decision.accepted
        original_get_all = store.get_all_contracts

        def mismatched_contracts():
            return [
                replace(contract, parent_id=None)
                if contract.id == descendant.id
                else contract
                for contract in original_get_all()
            ]

        monkeypatch.setattr(store, "get_all_contracts", mismatched_contracts)
        result = task_lineage(store, descendant.id)
        assert result["data"]["contract_ids"] == [descendant.id]
        assert not any(
            edge["kind"] == "containment"
            and edge["target_contract_id"] == descendant.id
            for edge in result["data"]["edges"]
        )
        assert any(
            issue["code"] == "contract_publication_unverified"
            and issue["contract_id"] == descendant.id
            for issue in result["data"]["issues"]
        )
    finally:
        store.close()


def test_late_source_failure_cannot_retroactively_qualify_recovery_publication(
    tmp_path, monkeypatch
):
    store, source, recovery, _publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        import aigineering.task_lineage as lineage_module

        original_loader = lineage_module.load_task_view_query_context

        def reorder_recovery_receipt_before_failure(target_store):
            query = original_loader(target_store)
            recovery_receipt_ids = {
                record.id
                for _revision, record in query.runtime_records
                if record.record_type == "candidate.received"
                and record.payload.get("actor_id") == "plugin:recovery.publish.v1"
                and recovery.id
                in {
                    member.payload.get("contract", {}).get("id")
                    for _member_revision, member in query.runtime_records
                    if member.record_type == "contract.declared"
                    and member.payload.get("candidate_id")
                    == record.payload.get("candidate_id")
                }
            }
            failure_revision = next(
                revision
                for revision, record in query.runtime_records
                if record.record_type == "worker.invocation_failed"
                and record.payload.get("contract_id") == source.id
            )
            rows = tuple(
                (
                    failure_revision - 1
                    if record.id in recovery_receipt_ids
                    else revision,
                    record,
                )
                for revision, record in query.runtime_records
            )
            return replace(query, runtime_records=rows)

        monkeypatch.setattr(
            lineage_module,
            "load_task_view_query_context",
            reorder_recovery_receipt_before_failure,
        )
        result = task_lineage(store, recovery.id)
        assert result["data"]["contract_ids"] == [recovery.id]
        assert not any(
            edge["kind"] == "recovery_publication"
            and edge["target_contract_id"] == recovery.id
            for edge in result["data"]["edges"]
        )
        assert any(
            issue["code"] == "recovery_publication_unverified"
            and issue["contract_id"] == recovery.id
            for issue in result["data"]["issues"]
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    "parent_source", ["missing_declaration", "mismatched_materialization"]
)
def test_containment_requires_verified_parent_publication(
    tmp_path, monkeypatch, parent_source
):
    store, _source, recovery, publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        descendant = contract_from_fields(
            name="verified child",
            description="The child cannot pull an unverified parent into lineage.",
            outputs=("follow-up",),
            budget=1,
            parent_id=recovery.id,
            inputs=(),
            activation="",
            tool_scope=(),
            labels=(),
            origin="worker",
        )
        assert publisher.publish(
            (contract_declaration_effect(descendant),),
            idempotency_key=f"lineage-parent-{parent_source}",
        ).accepted
        if parent_source == "missing_declaration":
            original_scan = store.scan_runtime_records

            def without_parent_declaration():
                return tuple(
                    pair
                    for pair in original_scan()
                    if not (
                        pair[1].record_type == "contract.declared"
                        and pair[1].payload.get("contract", {}).get("id") == recovery.id
                    )
                )

            monkeypatch.setattr(
                store, "scan_runtime_records", without_parent_declaration
            )
        else:
            original_get_all = store.get_all_contracts
            monkeypatch.setattr(
                store,
                "get_all_contracts",
                lambda: [
                    replace(contract, name="unproven parent")
                    if contract.id == recovery.id
                    else contract
                    for contract in original_get_all()
                ],
            )
        result = task_lineage(store, descendant.id)
        assert result["data"]["contract_ids"] == [descendant.id]
        assert not any(
            edge["kind"] == "containment"
            and edge["target_contract_id"] == descendant.id
            for edge in result["data"]["edges"]
        )
        assert any(
            issue["code"] == "containment_parent_unverified"
            and issue["contract_id"] == descendant.id
            for issue in result["data"]["issues"]
        )
        parent_result = task_lineage(store, recovery.id)
        assert any(
            issue["code"] == "containment_parent_unverified"
            and issue["contract_id"] == descendant.id
            and issue["parent_contract_id"] == recovery.id
            for issue in parent_result["data"]["issues"]
        )
    finally:
        store.close()


def test_held_recovery_keeps_its_blocker_in_verified_lineage(tmp_path, monkeypatch):
    from aigineering.plugins.collaboration import COLLABORATION_EFFECTS
    from aigineering.protocol.effect_builders import task_block_effect

    store, source, recovery, publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        hold_publisher = CandidatePublisher(
            store,
            store,
            publisher.genesis,
            publisher.actor_key,
            publisher.signer,
            additional_effects=COLLABORATION_EFFECTS,
        )
        held = hold_publisher.publish(
            (task_block_effect(recovery.id, "operator is waiting for credentials"),),
            idempotency_key="hold-lineage-recovery",
        )
        assert held.accepted
        result = task_lineage(store, source.id)
        cards = {
            card["contract_id"]: card["task"] for card in result["data"]["contracts"]
        }
        assert cards[source.id]["status"] == "failed"
        assert cards[recovery.id]["status"] == "blocked"
        assert any(
            blocker.startswith("claim_hold:")
            for blocker in cards[recovery.id]["blockers"]
        )
        assert any(
            edge["kind"] == "recovery_publication"
            and edge["source_contract_id"] == source.id
            and edge["target_contract_id"] == recovery.id
            for edge in result["data"]["edges"]
        )
    finally:
        store.close()


def test_forged_schedule_is_only_an_unverified_observation(tmp_path, monkeypatch):
    store, source, recovery, _publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        forged = create_runtime_record(
            "worker_failure.recovery_scheduled",
            {"contract_id": source.id, "failure_id": "failure:forged"},
            causal_parents=("failure:forged",),
        )
        store.append_runtime_record(forged)
        result = task_lineage(store, source.id)
        assert recovery.id in result["data"]["contract_ids"]
        assert any(
            issue["code"] == "recovery_schedule_unverified"
            and issue["contract_id"] == source.id
            and issue["record_id"] == forged.id
            for issue in result["data"]["issues"]
        )
        diagnostic = next(
            item
            for item in result["data"]["source_diagnostics"]
            if item["contract_id"] == source.id
            and "unverified_schedule_observations" in item
        )
        assert diagnostic["unverified_schedule_observations"] == [
            {
                "record_id": forged.id,
                "revision": result["revision"],
                "record_type": "worker_failure.recovery_scheduled",
                "verification": "unverified_observation",
            }
        ]
        assert "failure:forged" not in json.dumps(result)
    finally:
        store.close()


def test_unverified_recovery_publication_remains_visible_from_source(
    tmp_path, monkeypatch
):
    store, source, recovery, _publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        import aigineering.task_lineage as lineage_module

        original_loader = lineage_module.load_task_view_query_context

        def wrong_recovery_actor(target_store):
            query = original_loader(target_store)
            rows = []
            for revision, record in query.runtime_records:
                if (
                    record.record_type == "candidate.received"
                    and record.payload.get("actor_id") == "plugin:recovery.publish.v1"
                ):
                    payload = deep_thaw(record.payload)
                    payload["actor_id"] = "actor:untrusted"
                    record = replace(record, payload=payload)
                rows.append((revision, record))
            return replace(query, runtime_records=tuple(rows))

        monkeypatch.setattr(
            lineage_module, "load_task_view_query_context", wrong_recovery_actor
        )
        result = task_lineage(store, source.id)
        assert recovery.id not in result["data"]["contract_ids"]
        assert any(
            issue["code"] == "recovery_publication_unverified"
            and issue["contract_id"] == recovery.id
            and issue["source_contract_id"] == source.id
            for issue in result["data"]["issues"]
        )
    finally:
        store.close()


def test_multiple_recovery_publications_keep_receipt_specific_schedule_proofs(
    tmp_path, monkeypatch
):
    store, source, first_recovery, publisher, host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        context_name = f"_fail_context_{source.id}"
        second_recovery = contract_from_fields(
            name="separately published recovery",
            description="A second target for the same source history.",
            outputs=source.outputs,
            budget=1,
            parent_id=None,
            inputs=(context_name,),
            activation="",
            tool_scope=(),
            labels=(),
            origin="recovery",
        )
        context_asset = Asset(
            id="proposal-only",
            name=context_name,
            content="safe source recovery context",
            created_by=source.id,
            promptable=True,
        )
        decision = publisher.publish(
            (
                contract_declaration_effect(second_recovery),
                asset_proposal_effect(context_asset),
            ),
            idempotency_key="lineage-second-recovery-publication",
            causal_parents=(source.id,),
        )
        assert decision.accepted, decision

        second_source = build_control_plane_contract(
            name="second independently failed source",
            description="A second source has its own signed failure and recovery.",
            outputs=("result",),
            budget=2,
        )
        assert publisher.publish(
            (contract_declaration_effect(second_source),),
            idempotency_key="lineage-second-failed-source",
        ).accepted
        second_claim = claim_next_package(
            store,
            worker_id=host.worker_id,
            contract_id=second_source.id,
            claim_candidate=host.claim_candidate(
                request_id="lineage-second-source-claim",
                contract_id=second_source.id,
            ),
        )
        assert second_claim is not None
        registry = CandidatePublisherRegistry((("recovery.publish.v1", publisher),))
        with pytest.raises(WorkerInvocationError):
            execute_claimed_package(
                second_claim,
                host,
                store,
                candidate_publishers=registry,
            )
        second_failure_recovery = next(
            contract
            for contract in store.get_all_contracts()
            if contract.origin == "recovery"
            and contract.parent_id is None
            and contract.name.startswith("second independently failed source")
        )

        import aigineering.task_lineage as lineage_module

        original_loader = lineage_module.load_task_view_query_context

        def reordered_loader(target_store):
            query = original_loader(target_store)
            rows = list(query.runtime_records)
            target_declarations = [
                pair
                for pair in rows
                if pair[1].record_type == "contract.declared"
                and pair[1].payload.get("contract", {}).get("id")
                in {
                    first_recovery.id,
                    second_recovery.id,
                    second_failure_recovery.id,
                }
            ]
            rows = [pair for pair in rows if pair not in target_declarations]
            rows.extend(reversed(target_declarations))
            return replace(query, runtime_records=tuple(rows))

        monkeypatch.setattr(
            lineage_module, "load_task_view_query_context", reordered_loader
        )
        result = task_lineage(store, source.id)
        recovery_edges = [
            edge
            for edge in result["data"]["edges"]
            if edge["kind"] == "recovery_publication"
        ]
        assert {edge["target_contract_id"] for edge in recovery_edges} == {
            first_recovery.id,
            second_recovery.id,
        }
        second_source_result = task_lineage(store, second_source.id)
        second_source_edges = [
            edge
            for edge in second_source_result["data"]["edges"]
            if edge["kind"] == "recovery_publication"
        ]
        assert {
            (edge["source_contract_id"], edge["target_contract_id"])
            for edge in second_source_edges
        } == {(second_source.id, second_failure_recovery.id)}
    finally:
        store.close()


@pytest.mark.parametrize("reverse_declarations", [False, True])
def test_preemptive_recovery_publication_never_qualifies_retroactively(
    tmp_path, monkeypatch, reverse_declarations
):
    early_publications = []

    def publish_preemptive(store, source, publisher, _host):
        context_name = f"_fail_context_{source.id}"
        early = contract_from_fields(
            name="preemptive recovery candidate",
            description="Published before this source has claimed or failed.",
            outputs=source.outputs,
            budget=1,
            parent_id=None,
            inputs=(context_name,),
            activation="",
            tool_scope=(),
            labels=(),
            origin="recovery",
        )
        decision = publisher.publish(
            (
                contract_declaration_effect(early),
                asset_proposal_effect(
                    Asset(
                        id="proposal-only",
                        name=context_name,
                        content="preemptive context",
                        created_by=source.id,
                        promptable=True,
                    )
                ),
            ),
            idempotency_key="lineage-preemptive-recovery-publication",
            causal_parents=(source.id,),
        )
        assert decision.accepted, decision
        early_publications.append(early)

    store, source, late_recovery, _publisher, _host = _signed_failed_recovery(
        tmp_path, monkeypatch, before_failure=publish_preemptive
    )
    early_recovery = early_publications[0]
    try:
        import aigineering.task_lineage as lineage_module

        original_loader = lineage_module.load_task_view_query_context

        def reordered_loader(target_store):
            query = original_loader(target_store)
            declarations = [
                pair
                for pair in query.runtime_records
                if pair[1].record_type == "contract.declared"
                and pair[1].payload.get("contract", {}).get("id")
                in {early_recovery.id, late_recovery.id}
            ]
            rows = [pair for pair in query.runtime_records if pair not in declarations]
            rows.extend(
                reversed(declarations) if reverse_declarations else declarations
            )
            return replace(query, runtime_records=tuple(rows))

        monkeypatch.setattr(
            lineage_module, "load_task_view_query_context", reordered_loader
        )
        expected_ids = {source.id, late_recovery.id}
        for selected_id in (source.id, late_recovery.id):
            result = task_lineage(store, selected_id)["data"]
            assert set(result["contract_ids"]) == expected_ids
            assert [
                (edge["source_contract_id"], edge["target_contract_id"])
                for edge in result["edges"]
                if edge["kind"] == "recovery_publication"
            ] == [(source.id, late_recovery.id)]
            assert any(
                issue["code"] == "recovery_publication_unverified"
                and issue["contract_id"] == early_recovery.id
                and issue["source_contract_id"] == source.id
                for issue in result["issues"]
            )
    finally:
        store.close()


def test_chained_recovery_publications_remain_navigable_from_both_ends(
    tmp_path, monkeypatch
):
    store, source, first_recovery, publisher, host = _signed_failed_recovery(
        tmp_path, monkeypatch
    )
    try:
        claimed = claim_next_package(
            store,
            worker_id=host.worker_id,
            contract_id=first_recovery.id,
            claim_candidate=host.claim_candidate(
                request_id="lineage-chain-claim", contract_id=first_recovery.id
            ),
        )
        assert claimed is not None
        with pytest.raises(WorkerInvocationError):
            execute_claimed_package(
                claimed,
                host,
                store,
                candidate_publishers=CandidatePublisherRegistry(
                    (("recovery.publish.v1", publisher),)
                ),
            )
        recoveries = [
            contract
            for contract in store.get_all_contracts()
            if contract.origin == "recovery"
        ]
        assert len(recoveries) == 2
        second_recovery = next(
            contract for contract in recoveries if contract.id != first_recovery.id
        )
        from_source = task_lineage(store, source.id)
        from_second = task_lineage(store, second_recovery.id)
        assert (
            from_source["data"]["contract_ids"] == from_second["data"]["contract_ids"]
        )
        recovery_edges = [
            edge
            for edge in from_source["data"]["edges"]
            if edge["kind"] == "recovery_publication"
        ]
        assert {
            (edge["source_contract_id"], edge["target_contract_id"])
            for edge in recovery_edges
        } == {(source.id, first_recovery.id), (first_recovery.id, second_recovery.id)}
    finally:
        store.close()


def test_lineage_cli_reports_required_redis_unavailable_without_fallback(
    monkeypatch,
):
    from aigineering.redis_collaboration import RedisCollaborationUnavailable

    calls = {"snapshot": 0, "close": 0, "collaboration": 0, "lineage": 0}

    class Snapshot:
        def close(self):
            calls["close"] += 1

    def snapshot():
        calls["snapshot"] += 1
        return Snapshot()

    monkeypatch.setattr(lineage_cli, "_persistent_snapshot", snapshot)

    def unavailable(_store):
        calls["collaboration"] += 1
        raise RedisCollaborationUnavailable("secret connection detail")

    monkeypatch.setattr(lineage_cli, "collaboration_service", unavailable)

    def forbidden_lineage(*_args, **_kwargs):
        calls["lineage"] += 1
        raise AssertionError("lineage read must not run after Redis failure")

    monkeypatch.setattr(lineage_cli, "task_lineage", forbidden_lineage)
    isolated_group = click.Group(name="task")
    lineage_cli.register_task_lineage_command(isolated_group)
    result = CliRunner().invoke(isolated_group, ["lineage", "task:v6:sample", "--json"])
    assert result.exit_code == 1
    envelope = json.loads(result.output)
    assert envelope == {"ok": False, "error": "redis_unavailable"}
    assert "secret connection detail" not in result.output
    assert calls == {"snapshot": 1, "close": 1, "collaboration": 1, "lineage": 0}


def test_lineage_cycle_detection_is_iterative_and_returns_only_cycle_members():
    path_edges = [
        {
            "source_contract_id": f"task:{index}",
            "target_contract_id": f"task:{index + 1}",
        }
        for index in range(1200)
    ]
    assert _cycle_members(path_edges) == set()
    path_edges.extend(
        (
            {
                "source_contract_id": "task:1198",
                "target_contract_id": "task:1199",
            },
            {
                "source_contract_id": "task:1199",
                "target_contract_id": "task:1198",
            },
        )
    )
    assert _cycle_members(path_edges) == {"task:1198", "task:1199"}
