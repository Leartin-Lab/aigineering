"""Consume public exact-input identity vectors and reject policy tampering."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from aigineering.protocol.identity import (
    contract_from_fields,
    validate_contract_identity,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.wire import contract_from_dict, contract_to_dict


def test_public_exact_input_binding_identities():
    vectors = json.loads(
        Path("conformance/v0.5.13/input-binding-vectors.json").read_text()
    )
    for item in vectors["contract_identity_vectors"]:
        contract = contract_from_dict(item["value"])
        validate_contract_identity(contract)
        fields = deep_thaw(contract_to_dict(contract))
        fields.pop("id")
        assert contract_from_fields(**fields).id == contract.id
        policy = deep_thaw(contract.sensitive_input_policy)
        policy["input_asset_bindings"] = {"unexpected-role": []}
        with pytest.raises(ValueError, match="canonical v6 identity"):
            validate_contract_identity(replace(contract, sensitive_input_policy=policy))
