"""The fixed effect set cannot be extended with arbitrary fact producers."""

import inspect
from aigineering.core import effect_projection
from aigineering.core.commitment import CandidateCommitter


def test_core_effect_surface_has_no_injected_fact_registry():
    source = inspect.getsource(effect_projection)
    assert "aigineering.plugins" not in source
    assert not hasattr(effect_projection, "EffectHandler")
    assert not hasattr(effect_projection, "freeze_additional_effects")
    assert "additional_effects" not in inspect.signature(CandidateCommitter).parameters
