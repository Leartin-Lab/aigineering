"""Pure container validation for shared publication history readers."""

import pytest

from aigineering.core.publication_history import read_effect_types


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (("contract.declare", "asset.propose"), ("contract.declare", "asset.propose")),
        (["asset.propose", "contract.declare"], ("asset.propose", "contract.declare")),
        ([], ()),
    ],
)
def test_effect_types_reader_preserves_ordered_sequences(value, expected):
    assert read_effect_types({"effect_types": value}) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"effect_types": {"contract.declare": "ignored"}},
        {"effect_types": "contract.declare"},
        {"effect_types": None},
        {"effect_types": ["contract.declare", 7]},
        None,
    ],
)
def test_effect_types_reader_rejects_noncanonical_containers(payload):
    with pytest.raises(ValueError, match="publication effect types are not a sequence"):
        read_effect_types(payload)
