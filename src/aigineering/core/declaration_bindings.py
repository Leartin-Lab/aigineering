"""Compile signed batch-local Asset references into an ordinary Contract."""

from __future__ import annotations

from collections.abc import Mapping

from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.effect_projectors import (
    _contract_from_payload,
    proposed_asset_identity,
    proposed_asset_creator,
)
from aigineering.core.input_bindings import input_asset_bindings
from aigineering.protocol.effect_builders import LOCAL_ASSET_REFERENCE_PREFIX
from aigineering.protocol.identity import CONTRACT_SELF_REFERENCE, contract_from_fields
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.wire import contract_to_dict


def bind_declaration_assets(
    effect, candidate, effect_index, *, projected_assets=None, asset_id_cache=None
):
    """Resolve only three frozen Asset-ID positions, never names or arbitrary JSON.

    Read hints calculate scalar IDs. Admission uses already projected Assets;
    neither path creates work or changes permissions. Replay calls neither.
    """
    if (
        effect.effect_type != "contract.declare"
        or "bind_local_assets" not in effect.payload
    ):
        return None
    if effect.payload["bind_local_assets"] is not True:
        raise ValueError("bind_local_assets must be true")
    template = _contract_from_payload(effect.payload)
    validate_contract_commitment(template)
    bindings = input_asset_bindings(template.sensitive_input_policy)
    ids = asset_id_cache if asset_id_cache is not None else {}
    if not any(
        value.startswith(LOCAL_ASSET_REFERENCE_PREFIX)
        for role in template.inputs
        for value in bindings.get(role, ())
    ):
        # An older writer must reject the signed template at its existing exact
        # input fence, rather than silently ignoring the new compilation marker.
        raise ValueError("bind_local_assets requires an active local input binding")

    def resolve(value):
        if not isinstance(value, str):
            raise ValueError("local Asset bindings require string references")
        if not value.startswith(LOCAL_ASSET_REFERENCE_PREFIX):
            return value
        digits = value.removeprefix(LOCAL_ASSET_REFERENCE_PREFIX)
        if not digits.isascii() or not digits.isdecimal() or str(int(digits)) != digits:
            raise ValueError("invalid local Asset effect index")
        index = int(digits)
        if (
            index >= effect_index
            or candidate.effects[index].effect_type != "asset.propose"
        ):
            raise ValueError(
                "local Asset reference must identify a prior asset.propose"
            )
        if projected_assets is not None:
            asset = projected_assets.get(index)
            if asset is None:
                raise ValueError("local Asset reference has no projected Asset")
            return asset.id
        if index in ids:
            return ids[index]
        data = candidate.effects[index].payload.get("asset")
        if not isinstance(data, Mapping):
            raise ValueError("local Asset reference requires an Asset proposal")
        data = deep_thaw(data)
        ids[index] = proposed_asset_identity(
            candidate, data, proposed_asset_creator(candidate, data)
        )
        return ids[index]

    fields = contract_to_dict(template)
    fields.pop("id")
    policy = fields["sensitive_input_policy"]
    policy["input_asset_bindings"] = {
        role: sorted(resolve(value) for value in values)
        if role in template.inputs
        else list(values)
        for role, values in bindings.items()
    }
    fields["context_asset_ids"] = sorted(
        resolve(value) for value in template.context_asset_ids
    )
    if (
        fields["acceptance_policy"] is not None
        and "evidence_asset_ids" in fields["acceptance_policy"]
    ):
        fields["acceptance_policy"]["evidence_asset_ids"] = sorted(
            resolve(value)
            for value in fields["acceptance_policy"]["evidence_asset_ids"]
        )
    fields["minting_authority"] = [
        value.replace(template.id, CONTRACT_SELF_REFERENCE)
        for value in template.minting_authority
    ]
    return contract_from_fields(**fields)
