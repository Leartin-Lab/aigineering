from __future__ import annotations

from pathlib import Path

from aigineering.core.effect_projection import EffectHandler
from aigineering.plugins.worker_profile_promotion import (
    WORKER_PROFILE_PROMOTION_EFFECTS,
)


ROOT = Path(__file__).resolve().parents[2]


def test_profile_promotion_is_injected_and_does_not_override_builtin_registration():
    assert set(WORKER_PROFILE_PROMOTION_EFFECTS) == {"worker.profile.promote"}
    handler = WORKER_PROFILE_PROMOTION_EFFECTS["worker.profile.promote"]
    assert isinstance(handler, EffectHandler)
    assert handler.capability == "worker.register"
    assert handler.emitted_fact_types == frozenset()
    assert handler.direct_output_fact_types == frozenset({"worker.profile.promoted"})
    assert handler.reserved_fact_types == frozenset()
    assert handler.projector.__name__ == "project_worker_profile_promotion"


def test_generic_core_does_not_know_profile_promotion_effect_or_fact_names():
    files = (
        "src/aigineering/core/effect_projection.py",
        "src/aigineering/core/effect_projectors.py",
        "src/aigineering/core/commitment.py",
        "src/aigineering/core/commit_snapshot.py",
        "src/aigineering/core/sqlite_store.py",
        "src/aigineering/core/worker_coordination.py",
    )
    core = "\n".join((ROOT / path).read_text(encoding="utf-8") for path in files)
    assert "worker.profile.promote" not in core
    assert "worker.profile.promoted" not in core
    assert "from aigineering.plugins" not in core


def test_local_publisher_injects_promotion_without_widening_global_publisher():
    source = (ROOT / "src/aigineering/local_identity.py").read_text(encoding="utf-8")
    assert "additional_effects=WORKER_PROFILE_PROMOTION_EFFECTS" in source
    assert "CandidatePublisher(" in source
    assert "WORKER_PROFILE_PROMOTION_EFFECTS" not in (
        ROOT / "src/aigineering/core/candidate_publisher.py"
    ).read_text(encoding="utf-8")
