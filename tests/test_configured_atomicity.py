"""Behavioral coverage for atomic configured root publication."""

from __future__ import annotations

from dataclasses import replace

import pytest

from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_runtime_publishers
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.local_publication import commit_local_effects, require_accepted
from aigineering.run_config import prepare_configured_run


def _prepared_run(tmp_path, *, skill=True, evidence=True):
    (tmp_path / "input.txt").write_text("input bytes: 雪\n", encoding="utf-8")
    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path="store.db"\n'
        '[[workers]]\nid="configured-test-worker"\nkind="llm"\nmodel="unused"\n',
        encoding="utf-8",
    )
    skills = 'skills=["skill"]\n' if skill else ""
    if skill:
        (tmp_path / "skill").mkdir()
        (tmp_path / "skill" / "skill.toml").write_text(
            'name="guide"\nversion="1"\n', encoding="utf-8"
        )
        (tmp_path / "skill" / "skill.md").write_text(
            "Skill text with 雪.", encoding="utf-8"
        )
    evidence_line = 'acceptance_evidence_inputs=["source"]\n' if evidence else ""
    preset = tmp_path / "run.toml"
    preset.write_text(
        '[run]\nfleet="workers.toml"\n'
        + skills
        + '[[inputs]]\nname="source"\npath="input.txt"\n'
        + '[task]\nname="atomic-root"\ninputs=["source"]\n'
        + 'outputs=["answer"]\nlabels=["_skill_content_guide"]\n'
        + evidence_line
        + '[task.acceptance_policy]\nmode="independent"\npolicy_version="test-v1"\n',
        encoding="utf-8",
    )
    return prepare_configured_run(preset)


def _bootstrap(store, tmp_path, monkeypatch):
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(tmp_path / "identity.ed25519"))
    ensure_local_runtime_publishers(store)


def test_configured_assets_and_root_share_one_candidate_and_exact_bindings(
    tmp_path, monkeypatch
):
    import aigineering.configured_run as configured_run

    monkeypatch.chdir(tmp_path)
    prepared = _prepared_run(tmp_path)
    source_asset = replace(
        prepared.input_assets[0],
        promptable=False,
        content_type="text/x-configured-source",
        source_uri="configured:test/source",
    )
    unused_skill = build_control_plane_asset(
        name="unused-context", content="not selected", promptable=False
    )
    prepared = replace(
        prepared,
        input_assets=(source_asset,),
        skill_assets=(*prepared.skill_assets, unused_skill),
    )
    store = SQLiteStore(tmp_path / "store.db")
    _bootstrap(store, tmp_path, monkeypatch)

    # A same-name historical Asset must not change which prepared Asset is bound.
    require_accepted(
        commit_local_effects(
            store,
            (
                asset_proposal_effect(
                    build_control_plane_asset(
                        name="source", content="old same-name source"
                    )
                ),
            ),
            idempotency_key="configured-atomicity-old-source",
        )
    )
    commits = []
    real_commit = configured_run.commit_local_effects

    def capture_business_commit(*args, **kwargs):
        decision = real_commit(*args, **kwargs)
        commits.append(decision)
        return decision

    monkeypatch.setattr(configured_run, "commit_local_effects", capture_business_commit)
    try:
        root = configured_run._publish_root(store, prepared)
        assert len(commits) == 1
        assert commits[0].accepted
        assert commits[0].contract.id == root.id
        receipts = [
            record
            for record in commits[0].runtime_records
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id") == commits[0].candidate_id
        ]
        heads = [
            record
            for record in commits[0].runtime_records
            if record.record_type == "candidate.committed"
            and record.payload.get("candidate_id") == commits[0].candidate_id
        ]
        assert len(receipts) == len(heads) == 1
        head = heads[0]
        member_ids = tuple(head.payload["committed_record_ids"])
        assert receipts[0].id in head.causal_parents
        assert set(member_ids) <= set(head.causal_parents)
        members = [store.get_runtime_record(record_id) for record_id in member_ids]
        assert all(record is not None for record in members)
        asset_members = [
            record for record in members if record.record_type == "asset.committed"
        ]
        assert {record.payload["asset"]["id"] for record in asset_members} == {
            asset.id for asset in commits[0].assets
        }
        assert sum(record.record_type == "contract.declared" for record in members) == 1
        assert {record.payload["asset"]["name"] for record in asset_members} == {
            "source",
            "_skill_content_guide",
            "_skill_capability_guide",
            "unused-context",
        }
        assert any(
            record.payload["contract"]["id"] == root.id
            for record in members
            if record.record_type == "contract.declared"
        )
        assets_by_name = {asset.name: asset for asset in commits[0].assets}
        source = assets_by_name["source"]
        skill = assets_by_name["_skill_content_guide"]
        assert source.content == "input bytes: 雪\n"
        assert skill.content == "Skill text with 雪."
        assert source.promptable is False
        assert source.content_type == "text/x-configured-source"
        assert source.source_uri == "configured:test/source"
        assert root.sensitive_input_policy["input_asset_bindings"]["source"] == (
            source.id,
        )
        assert root.context_asset_ids == (skill.id,)
        assert assets_by_name["unused-context"].id not in root.context_asset_ids
        assert root.acceptance_policy["evidence_asset_ids"] == (source.id,)
        assert store.get_contract(root.id) == root
        assert len(store.get_assets_by_name("source")) == 2
    finally:
        store.close()


@pytest.mark.parametrize(
    "bad_label", ["_skill_content_missing", "_skill_content_guide"]
)
def test_bad_configured_binding_is_rejected_before_resource_commit(
    tmp_path, monkeypatch, bad_label
):
    import aigineering.configured_run as configured_run

    monkeypatch.chdir(tmp_path)
    prepared = _prepared_run(tmp_path)
    contract = prepared.contract
    assert contract is not None
    if bad_label == "_skill_content_guide":
        # An explicit active input binding conflicts with the generated source binding.
        policy = dict(contract.sensitive_input_policy or {})
        policy["input_asset_bindings"] = {"source": ("asset:explicit-other",)}
        prepared = replace(
            prepared,
            contract=replace(contract, sensitive_input_policy=policy),
        )
    else:
        prepared = replace(prepared, contract=replace(contract, labels=(bad_label,)))

    store = SQLiteStore(tmp_path / "store.db")
    _bootstrap(store, tmp_path, monkeypatch)
    before = store.get_runtime_revision()
    try:
        with pytest.raises(ValueError):
            configured_run._publish_root(store, prepared)
        assert store.get_runtime_revision() == before
        assert not store.get_assets_by_name("source")
        assert not store.get_assets_by_name("_skill_content_guide")
        assert not store.get_all_contracts()
    finally:
        store.close()


def test_late_root_admission_rejection_does_not_commit_prepared_assets(
    tmp_path, monkeypatch
):
    import aigineering.configured_run as configured_run

    monkeypatch.chdir(tmp_path)
    prepared = _prepared_run(tmp_path)
    store = SQLiteStore(tmp_path / "store.db")
    _bootstrap(store, tmp_path, monkeypatch)
    before = store.get_runtime_revision()
    real_commit = configured_run.commit_local_effects
    decisions = []

    def reject_unmarked_local_bindings(store_arg, effects, **kwargs):
        effects = list(effects)
        declaration = effects[-1]
        payload = dict(declaration.payload)
        payload["bind_local_assets"] = False
        effects[-1] = replace(declaration, payload=payload)
        decision = real_commit(store_arg, tuple(effects), **kwargs)
        decisions.append(decision)
        return decision

    monkeypatch.setattr(
        configured_run, "commit_local_effects", reject_unmarked_local_bindings
    )
    try:
        with pytest.raises(ValueError, match="bind_local_assets must be true"):
            configured_run._publish_root(store, prepared)
        assert len(decisions) == 1 and not decisions[0].accepted
        rejection = next(
            record
            for record in decisions[0].runtime_records
            if record.record_type == "candidate.rejected"
        )
        assert rejection.payload["reason"] == "bind_local_assets must be true"
        assert store.get_runtime_revision() > before  # authenticated rejection audit
        assert not store.get_assets_by_name("source")
        assert not store.get_assets_by_name("_skill_content_guide")
        assert not store.get_all_contracts()
    finally:
        store.close()


def test_candidate_transaction_interruption_leaves_no_resources_or_contract(
    tmp_path, monkeypatch
):
    import aigineering.configured_run as configured_run

    monkeypatch.chdir(tmp_path)
    prepared = _prepared_run(tmp_path)
    store = SQLiteStore(tmp_path / "store.db")
    _bootstrap(store, tmp_path, monkeypatch)
    before = store.get_runtime_revision()
    original_insert = SQLiteStore._insert_runtime_record

    def interrupt_candidate(self, record):
        if record.record_type == "candidate.committed":
            raise RuntimeError("injected configured publication interruption")
        return original_insert(self, record)

    monkeypatch.setattr(SQLiteStore, "_insert_runtime_record", interrupt_candidate)
    try:
        with pytest.raises(RuntimeError, match="injected configured publication"):
            configured_run._publish_root(store, prepared)
        assert store.get_runtime_revision() == before
        assert not store.get_assets_by_name("source")
        assert not store.get_assets_by_name("_skill_content_guide")
        assert not store.get_all_contracts()
    finally:
        store.close()


def test_atomic_configured_publication_survives_reopen_and_strict_rebuild(
    tmp_path, monkeypatch
):
    import aigineering.configured_run as configured_run

    monkeypatch.chdir(tmp_path)
    prepared = _prepared_run(tmp_path)
    db_path = tmp_path / "store.db"
    store = SQLiteStore(db_path)
    _bootstrap(store, tmp_path, monkeypatch)
    root = configured_run._publish_root(store, prepared)
    digest = store.runtime_materialization_digest()
    revision = store.get_runtime_revision()
    store.close()

    import shutil

    copy_path = tmp_path / "strict-copy.db"
    shutil.copy2(db_path, copy_path)
    copied = SQLiteStore(copy_path)
    try:
        assert copied.get_contract(root.id) == root
        assert copied.rebuild_runtime_materializations(strict=True) == digest
        assert len(copied.get_assets_by_name("source")) == 1
        assert len(copied.get_assets_by_name("_skill_content_guide")) == 1
    finally:
        copied.close()

    reopened = SQLiteStore(db_path)
    try:
        assert reopened.get_contract(root.id) == root
        assert reopened.runtime_materialization_digest() == digest
        assert reopened.get_runtime_revision() == revision
    finally:
        reopened.close()
