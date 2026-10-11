from __future__ import annotations

import inspect

from aigineering.core import effect_projection, injected_fact_replay


def test_fact_ownership_core_is_feature_neutral_and_explicitly_opt_in():
    projection_source = inspect.getsource(effect_projection)
    replay_source = inspect.getsource(injected_fact_replay)
    assert "aigineering.plugins" not in projection_source
    assert "aigineering.plugins" not in replay_source
    assert "task.priority" not in projection_source
    assert "task.priority" not in replay_source
    assert "direct_output_fact_types" in projection_source
    assert "exact_single_effect_head_replay" in projection_source


def test_effect_handler_keeps_strict_fields_keyword_only():
    parameters = inspect.signature(effect_projection.EffectHandler).parameters
    assert parameters["direct_output_fact_types"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["reserved_fact_types"].kind is inspect.Parameter.KEYWORD_ONLY
