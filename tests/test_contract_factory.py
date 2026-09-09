from aigineering.core.ids import (
    CONTRACT_SELF_REFERENCE,
    contract_from_fields,
    hash_contract_current,
    validate_contract_identity,
)


def _fields(**overrides):
    fields = {
        "name": "canonical",
        "description": "canonical construction",
        "inputs": ["input"],
        "outputs": ["output"],
        "activation": "input",
        "budget": 2,
        "tool_scope": ["read"],
        "labels": ["test"],
        "origin": "system",
        "parent_id": "task:parent",
        "worker_capabilities": ["worker"],
        "worker_pools": ["pool"],
        "minting_authority": [
            "output",
            f"_system_{CONTRACT_SELF_REFERENCE}",
        ],
        "sensitive_input_policy": {"input": {"disclose": True}},
    }
    fields.update(overrides)
    return fields


def test_contract_from_fields_preserves_current_identity_and_expands_self_reference():
    fields = _fields()

    contract = contract_from_fields(**fields)

    assert contract.id == hash_contract_current(**fields)
    assert contract.id.startswith("task:v3:")
    assert contract.minting_authority == (
        "output",
        f"_system_{contract.id}",
    )
    assert fields["minting_authority"][1] == f"_system_{CONTRACT_SELF_REFERENCE}"
    validate_contract_identity(contract)


def test_contract_from_fields_keeps_hash_contract_current_version_selection():
    v4 = contract_from_fields(**_fields(context_asset_ids=["asset:context"]))
    v5 = contract_from_fields(
        **_fields(
            context_asset_ids=["asset:context"],
            delegation_capabilities=["delegate"],
        )
    )

    assert v4.id.startswith("task:v4:")
    assert v5.id.startswith("task:v5:")
    validate_contract_identity(v4)
    validate_contract_identity(v5)
