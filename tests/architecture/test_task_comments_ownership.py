from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_comment_semantics_stay_in_plugin_and_commit_through_candidate_boundary():
    plugin = (ROOT / "src/aigineering/plugins/collaboration.py").read_text()
    projector = (ROOT / "src/aigineering/core/effect_projectors.py").read_text()
    builders = (ROOT / "src/aigineering/protocol/effect_builders.py").read_text()
    projection = (ROOT / "src/aigineering/core/effect_projection.py").read_text()
    committer = (ROOT / "src/aigineering/core/commitment.py").read_text()
    publisher = (ROOT / "src/aigineering/core/candidate_publisher.py").read_text()

    assert "SQLiteStore" not in plugin
    assert "CandidateCommitter" not in plugin
    assert "commit_ingress_batch" not in plugin
    assert "append_runtime_record" not in plugin
    assert "COLLABORATION_EFFECTS" in plugin
    assert (
        '"task.comment": ("task.comment", project_task_comment_effect)' not in projector
    )
    assert '"task.comment"' in builders
    assert "additional_effects" in projection
    assert "additional_effects" in committer
    assert "additional_effects" in publisher
