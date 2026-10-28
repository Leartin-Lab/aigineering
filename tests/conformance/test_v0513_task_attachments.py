"""Consume the public cross-language task-attachment effect vector."""

import json
from pathlib import Path

from aigineering.core.ids import canonical_json
from aigineering.protocol.candidate import CandidateEffect


def test_public_task_attachment_effect_vector():
    vector = json.loads(
        Path("conformance/v0.5.13/task-attachment-vectors.json").read_text(
            encoding="utf-8"
        )
    )
    effect = CandidateEffect(
        vector["effect"]["effect_type"], vector["effect"]["payload"]
    )
    assert (
        canonical_json(
            {"effect_type": effect.effect_type, "payload": dict(effect.payload)}
        )
        == vector["expected_canonical_json"]
    )
