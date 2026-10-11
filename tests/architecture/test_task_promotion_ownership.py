from __future__ import annotations

from pathlib import Path

from aigineering.core.effect_projection import EffectHandler
from aigineering.plugins.collaboration import COLLABORATION_EFFECTS


ROOT = Path(__file__).parents[2]


def test_task_promotion_is_plugin_owned_and_only_releases_holds():
    handler = COLLABORATION_EFFECTS["task.promote"]
    assert isinstance(handler, EffectHandler)
    assert handler.capability == "task.unblock"
    assert handler.direct_output_fact_types == frozenset(
        {"claim.hold.released", "task.promoted"}
    )
    assert handler.reserved_fact_types == frozenset({"task.promoted"})

    core = (ROOT / "src/aigineering/core/claim_holds.py").read_text(encoding="utf-8")
    projection = (ROOT / "src/aigineering/core/effect_projection.py").read_text(
        encoding="utf-8"
    )
    plugin = (ROOT / "src/aigineering/plugins/collaboration.py").read_text(
        encoding="utf-8"
    )
    assert "task.promote" not in core
    assert '"task.promoted"' not in core
    assert "task.promote" not in projection
    assert "validate_task_promote_request" in plugin
