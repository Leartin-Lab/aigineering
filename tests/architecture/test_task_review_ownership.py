from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_review_semantics_live_in_plugin_and_delegate_canonical_acceptance():
    plugin = (ROOT / "src/aigineering/plugins/task_review.py").read_text()
    acceptance = (ROOT / "src/aigineering/core/acceptance.py").read_text()
    core = "\n".join(
        path.read_text() for path in (ROOT / "src/aigineering/core").glob("*.py")
    )
    assert '"task.review.request":' in plugin
    assert '"task.review.decide":' in plugin
    assert "def is_task_review_source_eligible(source: Contract) -> bool:" in plugin
    assert "if not is_task_review_source_eligible(source):" in plugin
    assert "project_asset_attestation_records(" in plugin
    assert "asset_attestation_effect(" in plugin
    assert "task.review.request" not in core
    assert "task.review.decide" not in core
    assert (
        "CREATE TABLE review"
        not in (ROOT / "src/aigineering/core/sqlite_schema.py").read_text()
    )
    assert "task.review" not in acceptance
    policy_review = (ROOT / "src/aigineering/task_policy_review.py").read_text()
    assert "is_task_review_source_eligible," in policy_review
    assert "if not is_task_review_source_eligible(source):" in policy_review


def test_review_contract_and_links_are_ordinary_immutable_runtime_facts():
    plugin = (ROOT / "src/aigineering/plugins/task_review.py").read_text()
    assert "contract_from_fields(" in plugin
    assert "context_asset_ids=context_ids" in plugin
    assert 'origin="human" if source.origin == "retry" else source.origin' in plugin
    assert '"task.review.requested"' in plugin
    assert '"task.review.decided"' in plugin
    assert (
        "CREATE TABLE review"
        not in (ROOT / "src/aigineering/core/sqlite_migrations.py").read_text()
    )


def test_task_output_claim_provenance_has_one_shared_acceptance_owner():
    acceptance = (ROOT / "src/aigineering/core/acceptance.py").read_text()
    plugin = (ROOT / "src/aigineering/plugins/task_review.py").read_text()
    views = (ROOT / "src/aigineering/task_views.py").read_text()
    policy_review = (ROOT / "src/aigineering/task_policy_review.py").read_text()
    assert "def validate_claim_bound_output(" in acceptance
    assert "validate_published_member(rows, member)" in acceptance
    assert "validate_claim_bound_output(" in plugin
    assert "validate_claim_bound_output(" in views
    assert "validate_claim_bound_output(records, asset, producer)" in policy_review
