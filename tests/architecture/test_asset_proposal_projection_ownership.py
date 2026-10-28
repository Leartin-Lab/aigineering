from __future__ import annotations

import inspect

import pytest

from aigineering.core.effect_projection import (
    BUILTIN_EFFECTS,
    freeze_additional_effects,
)
from aigineering.core.effect_projectors import project_asset_proposal
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.projection_context import (
    AssetProposalProjectionInputs,
    AssetProposalProjectionRequest,
    load_asset_proposal_projection_context,
    load_candidate_projection_context,
)
from aigineering.core.store import CandidateCommitStore, StoreProtocol
from aigineering.core.sqlite_store import SQLiteStore


def test_asset_projection_reader_is_optional_and_sql_stays_in_sqlite_adapter():
    method_name = "read_asset_proposal_projection_inputs"
    assert not hasattr(StoreProtocol, method_name)
    assert not hasattr(CandidateCommitStore, method_name)
    assert method_name in SQLiteStore.__dict__
    assert "SELECT" not in inspect.getsource(load_asset_proposal_projection_context)
    source = inspect.getsource(project_asset_proposal)
    assert "store." not in source
    assert "get_all_" not in source
    assert AssetProposalProjectionRequest.__dataclass_params__.frozen
    assert AssetProposalProjectionInputs.__dataclass_params__.frozen


def test_builtin_asset_propose_cannot_be_overridden_by_injected_effects():
    with pytest.raises(ValueError, match="cannot replace built-in 'asset.propose'"):
        freeze_additional_effects(
            {"asset.propose": ("asset.publish", lambda *_args: None)}
        )
    assert BUILTIN_EFFECTS["asset.propose"][0] == "asset.publish"


def test_commitment_dispatch_stays_before_authentication_and_reduction_owners():
    source = inspect.getsource(CandidateCommitter.commit)
    dispatch = source.index("load_candidate_projection_context")
    reduction = source.index("reduce_candidate(")
    authentication_rejection = source.index(
        "record_unreceipted_authentication_rejection("
    )
    assert source.count("load_candidate_projection_context(") == 1
    assert "build_asset_proposal_projection_request" not in source
    assert "load_asset_proposal_projection_context" not in source
    assert "load_contract_declaration_projection_context" not in source
    assert dispatch < reduction < authentication_rejection


def test_generic_context_facade_and_direct_loaders_are_owned_by_projection_context():
    from aigineering.core import projection_context

    assert callable(load_candidate_projection_context)
    assert callable(load_asset_proposal_projection_context)
    assert callable(projection_context.load_contract_declaration_projection_context)
    assert "SELECT" not in inspect.getsource(load_candidate_projection_context)


def test_sql_selectors_are_owned_by_the_sqlite_reader():
    from aigineering.core import projection_context

    projection_source = inspect.getsource(projection_context)
    assert "SELECT * FROM runtime_records" not in projection_source
    assert "SELECT * FROM runtime_records" in inspect.getsource(
        SQLiteStore.read_asset_proposal_projection_inputs
    ) or "SELECT * FROM runtime_records" in inspect.getsource(
        SQLiteStore._read_asset_proposal_projection_inputs
    )
