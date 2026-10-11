from __future__ import annotations

from pathlib import Path

import pytest

from aigineering.run_config import prepare_configured_run


REPO_ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    ("example", "inputs", "output", "context", "policy_version"),
    [
        (
            "ai4s",
            ("literature_query",),
            "literature_report",
            "_skill_content_literature_evidence",
            "ai4s-literature-v1",
        ),
        (
            "report-consistency",
            ("report_source", "draft_report", "check_standard"),
            "audit_report",
            "_skill_content_report_consistency",
            "report-consistency-v1",
        ),
        (
            "claim-evidence-review",
            ("research_result",),
            "review_report",
            None,
            "claim-evidence-review-v1",
        ),
        (
            "guarded-claim-review",
            ("research_result", "draft_report", "review_policy", "review_instructions"),
            "review_report",
            None,
            "claim-review-closure-v1",
        ),
    ],
)
def test_published_example_presets_prepare_the_documented_root(
    tmp_path, monkeypatch, example, inputs, output, context, policy_version
):
    """Prepare checked-in declarations while isolating the workspace and adapters."""
    from aigineering import fleet_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    built = []

    def build_fixture(spec, *, registry=None):
        # In particular, guarded-claim-review's local factories must not be
        # invoked: preparation only needs to prove the configured specs exist.
        worker = object()
        built.append((spec, registry, worker))
        return worker

    monkeypatch.setattr(fleet_config, "build_fleet_worker", build_fixture)
    preset = REPO_ROOT / "examples" / example / "run.toml"

    prepared = prepare_configured_run(preset)

    contract = prepared.contract
    assert contract is not None
    assert prepared.source_path == preset.resolve()
    assert prepared.fleet_path == (preset.parent / "workers.toml").resolve()
    assert prepared.db_path == ".aig/store.db"
    assert tuple(asset.name for asset in prepared.input_assets) == inputs
    assert tuple(contract.inputs) == inputs
    assert tuple(contract.outputs) == (output,)
    assert contract.activation
    assert contract.budget >= 1
    assert contract.acceptance_policy["mode"] == "independent"
    assert contract.acceptance_policy["policy_version"] == policy_version

    # Preset-relative data files become the exact named input Assets, while
    # human-only ORACLE files stay outside the prepared prompt context.
    for asset in prepared.input_assets:
        source = (
            preset.parent
            / {
                "literature_query": "literature-query.json",
                "report_source": "source-data.json",
                "draft_report": "draft-report.json",
                "check_standard": "check-standard.json",
                "research_result": "research-result.json",
                "review_policy": "review-policy.json",
                "review_instructions": "review-instructions.txt",
            }[asset.name]
        )
        assert asset.content == source.read_text(encoding="utf-8")
        assert "ORACLE" not in asset.content

    if example == "claim-evidence-review":
        assert not prepared.skill_assets
        assert not contract.labels
    elif example == "guarded-claim-review":
        assert contract.labels == inputs
    else:
        content_asset = next(
            asset for asset in prepared.skill_assets if asset.name == context
        )
        assert context in contract.labels
        assert content_asset.content
        assert len(prepared.skill_assets) == 2
        assert any(asset.id == content_asset.id for asset in prepared.skill_assets)

    prepared_promptable_names = {
        asset.name
        for asset in (*prepared.input_assets, *prepared.skill_assets)
        if asset.promptable
    }
    assert set(contract.labels) <= prepared_promptable_names

    assert len(built) == len(prepared.fleet.workers) > 0
    assert len(prepared.workers) == len(built)
    assert all(
        actual_spec is expected_spec and actual_worker is expected_worker
        for (actual_spec, actual_worker), (
            expected_spec,
            _expected_registry,
            expected_worker,
        ) in zip(prepared.workers, built, strict=True)
    )
    assert not (tmp_path / ".aig").exists()

    if example == "ai4s":
        assert contract.tool_scope == ("openalex_search",)
        assert contract.worker_capabilities == ("planning",)
        assert contract.worker_pools == ("reasoning",)
        assert contract.delegation_capabilities == (
            "literature.retrieve",
            "literature.screen",
            "literature.extract",
            "literature.synthesize",
            "literature.replan",
            "literature.verify",
        )
        assert contract.delegation_pools == (
            "economy",
            "reasoning",
            "verification",
        )
        assert set(contract.acceptance_policy["output_shapes"][output]) == {
            "answer",
            "citations",
            "limitations",
        }
    elif example == "report-consistency":
        assert contract.tool_scope == ()
        assert contract.worker_capabilities == ("planning",)
        assert contract.worker_pools == ("reasoning",)
        assert contract.delegation_capabilities == (
            "report.extract",
            "report.check",
            "report.revise",
            "report.replan",
            "report.verify",
        )
        assert contract.delegation_pools == (
            "economy",
            "reasoning",
            "verification",
        )
        assert contract.acceptance_policy["verifier_capabilities"] == ("report.verify",)
        assert set(contract.acceptance_policy["output_shapes"][output]) == {
            "status",
            "corrected_total",
            "corrected_months",
            "findings",
            "source_asset_id",
            "report_id",
            "currency",
        }
    elif example == "claim-evidence-review":
        assert contract.tool_scope == ()
        assert contract.worker_capabilities == ("planning",)
        assert contract.worker_pools == ("reasoning",)
        assert contract.delegation_capabilities == (
            "claim.review.extract",
            "claim.review.check",
            "claim.review.revise",
            "claim.review.verify",
        )
        assert contract.delegation_pools == (
            "economy",
            "reasoning",
            "verification",
        )
        assert contract.acceptance_policy["verifier_capabilities"] == (
            "claim.review.verify",
        )
        assert set(contract.acceptance_policy["output_shapes"][output]) == {
            "claims",
            "revised_summary",
            "limitations",
        }
    elif example == "guarded-claim-review":
        assert prepared.acceptance_evidence_inputs == (
            "research_result",
            "draft_report",
            "review_policy",
        )
        assert contract.acceptance_policy["verifier_capabilities"] == (
            "claim.review.accept",
        )
        assert "review_instructions" not in prepared.acceptance_evidence_inputs
        assert contract.worker_capabilities == ("planning",)
        assert contract.worker_pools == ("planning",)
        assert contract.delegation_capabilities == (
            "claim.review.assess",
            "claim.review.revise",
            "claim.review.gate",
            "claim.review.semantic",
            "claim.review.accept",
        )
        assert contract.delegation_pools == (
            "validation",
            "reasoning",
            "verification",
        )


def test_invalid_preset_declaration_fails_before_adapter_or_workspace_creation(
    tmp_path, monkeypatch
):
    from aigineering import fleet_config

    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path=".aig/store.db"\n'
        '[[workers]]\nid="fixture"\nkind="llm"\nmodel="unused"\n',
        encoding="utf-8",
    )
    (tmp_path / "input.txt").write_text("source evidence", encoding="utf-8")
    (tmp_path / "run.toml").write_text(
        '[run]\nfleet="workers.toml"\n'
        '[[inputs]]\nname="source"\npath="input.txt"\n'
        '[task]\nname="bad-root"\ninputs=["source"]\noutputs=["source"]\n',
        encoding="utf-8",
    )
    key_path = tmp_path / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))

    def unexpected_build(_spec):
        pytest.fail("invalid declarations must fail before worker construction")

    monkeypatch.setattr(fleet_config, "build_fleet_worker", unexpected_build)

    with pytest.raises(ValueError, match="cannot reuse a declared input"):
        prepare_configured_run(tmp_path / "run.toml")

    assert not (tmp_path / ".aig").exists()
    assert not key_path.exists()
