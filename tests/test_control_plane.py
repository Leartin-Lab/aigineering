"""Pure control-plane proposal builder tests."""

import pytest

from aigineering.core.authority import RESERVED_PREFIXES, _is_protected_name
from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.contract_admission import validate_acceptance_policy
from aigineering.core.ids import hash_asset_content, hash_asset_definition


def test_asset_builder_preserves_proposal_metadata_and_hashes():
    asset = build_control_plane_asset(
        name="config",
        content="{}",
        origin="imported",
        trust_tier="configured",
        source_uri="file://config.json",
        promptable=False,
        content_type="application/json",
    )

    assert asset.id == hash_asset_content("config", "{}")
    assert asset.definition_hash == hash_asset_definition("config")
    assert asset.origin == "imported"
    assert asset.trust_tier == "configured"
    assert asset.source_uri == "file://config.json"
    assert asset.promptable is False
    assert asset.content_type == "application/json"
    assert asset.signed_by == ""


def test_asset_builder_rejects_every_reserved_prefix_without_explicit_intent():
    for prefix in sorted(RESERVED_PREFIXES):
        with pytest.raises(ValueError, match="protected prefix"):
            build_control_plane_asset(
                name=prefix + "example",
                content="test",
            )


def test_asset_builder_can_propose_protected_name_but_does_not_authorize_it():
    asset = build_control_plane_asset(
        name="_sys_admin_config",
        content="admin",
        allow_protected=True,
    )

    assert asset.name == "_sys_admin_config"
    assert asset.signed_by == ""


def test_reserved_prefix_detection_has_one_authority_source():
    for prefix in sorted(RESERVED_PREFIXES):
        assert _is_protected_name(prefix + "example")
    assert not _is_protected_name("normal_asset")
    assert not _is_protected_name("")


def test_contract_builder_creates_canonical_human_proposal():
    contract = build_control_plane_contract(
        name="build_report",
        description="Build a report",
        inputs=("data_file",),
        outputs=("final_report",),
        activation="data_file",
        budget=5,
        labels=("reviewed",),
        tool_scope=("lookup",),
    )

    assert contract.id.startswith("task:v3:")
    assert contract.origin == "human"
    assert contract.inputs == ("data_file",)
    assert contract.outputs == ("final_report",)
    assert contract.acceptance_policy == {
        "mode": "mechanical",
        "output_source": "contract_tree",
    }
    assert contract.labels == ("reviewed",)
    assert contract.tool_scope == ("lookup",)


def test_contract_builder_rejects_protected_output_and_authority_override():
    with pytest.raises(ValueError, match="protected"):
        build_control_plane_contract(name="bad", outputs=("_sys_config",))
    with pytest.raises(ValueError, match="cannot receive runtime minting authority"):
        build_control_plane_contract(
            name="bad",
            outputs=("_sys_config",),
            allow_protected_outputs=True,
        )


def test_contract_identity_is_deterministic():
    first = build_control_plane_contract(name="task", outputs=("out",), budget=3)
    second = build_control_plane_contract(name="task", outputs=("out",), budget=3)

    assert first == second


def test_explicit_legacy_mechanical_policy_is_preserved():
    contract = build_control_plane_contract(
        name="legacy compatibility",
        outputs=("out",),
        acceptance_policy={"mode": "mechanical"},
    )

    assert contract.acceptance_policy == {"mode": "mechanical"}


@pytest.mark.parametrize(
    "policy",
    (
        {"mode": "mechanical", "output_source": "other"},
        {"mode": "mechanical", "output_source": 1},
        {
            "mode": "independent",
            "policy_version": "review-v1",
            "output_source": "contract_tree",
        },
    ),
)
def test_acceptance_policy_rejects_unknown_or_incompatible_output_source(policy):
    contract = build_control_plane_contract(
        name="invalid policy",
        outputs=("out",),
        acceptance_policy=policy,
    )

    with pytest.raises(ValueError, match="output_source"):
        validate_acceptance_policy(contract)
