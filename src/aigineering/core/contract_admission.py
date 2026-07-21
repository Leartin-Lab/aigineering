"""Pure Contract admission policy shared by Candidate and compatibility paths."""

from __future__ import annotations

from aigineering.core.activation import validate_execution_activation
from aigineering.core.authority import matched_reserved_prefix
from aigineering.core.ids import validate_contract_identity
from aigineering.protocol.types import Contract


def validate_acceptance_policy(contract: Contract) -> None:
    policy = contract.acceptance_policy
    if policy is None:
        return
    mode = policy.get("mode")
    if mode not in {"mechanical", "independent"}:
        raise ValueError(
            "Contract acceptance_policy.mode must be 'mechanical' or 'independent'"
        )
    if mode == "independent":
        policy_version = policy.get("policy_version")
        if not isinstance(policy_version, str) or not policy_version:
            raise ValueError(
                "independent acceptance_policy.policy_version must be a string"
            )
    required = policy.get("required_attestations", 1)
    if required != 1 or isinstance(required, bool):
        raise ValueError(
            "v0.5 Contract acceptance_policy.required_attestations must equal 1"
        )
    for field_name in (
        "verifier_capabilities",
        "rubric_asset_ids",
        "evidence_asset_ids",
    ):
        values = policy.get(field_name, ())
        if not isinstance(values, (list, tuple)) or not all(
            isinstance(value, str) and value for value in values
        ):
            raise ValueError(f"Contract acceptance_policy.{field_name} must be strings")
        if tuple(values) != tuple(sorted(set(values))):
            raise ValueError(
                f"Contract acceptance_policy.{field_name} must be sorted and unique"
            )


def validate_contract_commitment(
    contract: Contract, *, require_canonical_v3: bool = True
) -> None:
    if require_canonical_v3 and not contract.id.startswith("task:v3:"):
        raise ValueError("Candidate contracts require a canonical task:v3 identity")
    validate_contract_identity(contract)
    validate_acceptance_policy(contract)
    validate_execution_activation(contract.activation)
    for output_name in contract.outputs:
        prefix = matched_reserved_prefix(output_name)
        if prefix is not None and output_name not in contract.minting_authority:
            raise ValueError(
                f"Contract output {output_name!r} uses protected prefix {prefix!r} "
                "without minting authority"
            )
