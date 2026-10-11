from pathlib import Path

from aigineering.plugins.handoff import HANDOFF_EFFECTS
from aigineering.core.effect_projection import EffectHandler


ROOT = Path(__file__).resolve().parents[2]


def test_task_edit_remains_inside_the_existing_handoff_effect_owner():
    handler = HANDOFF_EFFECTS["task.handoff"]
    assert isinstance(handler, EffectHandler)
    assert handler.projector.__module__ == "aigineering.plugins.handoff"

    core_sources = "\n".join(
        (ROOT / "src/aigineering/core" / name).read_text(encoding="utf-8")
        for name in (
            "commitment.py",
            "commit_snapshot.py",
            "effect_projection.py",
            "sqlite_store.py",
        )
    )
    assert "task.edit" not in core_sources
    assert "task.handoff" not in core_sources
    assert "task.edit" in Path(ROOT / "src/aigineering/plugins/handoff.py").read_text(
        encoding="utf-8"
    )
