"""Review is a primitive compiler, never a fact projector or core task gate."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_review_uses_canonical_primitives_without_an_injected_effect_owner():
    plugin = (ROOT / "src/aigineering/plugins/task_review.py").read_text()
    service = (ROOT / "src/aigineering/task_policy_review.py").read_text()
    core = "\n".join(
        path.read_text() for path in (ROOT / "src/aigineering/core").glob("*.py")
    )
    assert "contract_declaration_effect(" in service
    assert "asset_attestation_effect(" in service
    assert "asset_proposal_effect(" in service
    for removed in (
        "TASK_REVIEW_EFFECTS",
        "EffectHandler",
        "is_task_review_source_eligible",
    ):
        assert removed not in plugin + service
    assert "task.review.request" not in core
    assert "task.review.decide" not in core
    assert "validate_asset_publication(" in plugin + service
    assert "validate_claim_bound_output(" not in plugin + service
