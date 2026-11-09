from __future__ import annotations

import inspect

from aigineering.core.effect_projection import (
    BUILTIN_EFFECTS,
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


def test_builtin_asset_propose_has_no_injection_registry():
    assert BUILTIN_EFFECTS["asset.propose"][0] == "asset.publish"
    assert "additional_effects" not in inspect.signature(CandidateCommitter).parameters


def test_commitment_authenticates_before_readonly_replay_and_fresh_projection():
    source = inspect.getsource(CandidateCommitter._commit_locked)
    assert source.index("candidate_received_record(") < source.index(
        "read_candidate_publication("
    )
    assert source.index("read_candidate_publication(") < source.index(
        "load_candidate_projection_context("
    )
    assert source.count("_reduce_received_candidate(") == 1
    assert source.count("commit_ingress_batch(") == 1


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
    assert "self.get_contract(" in inspect.getsource(
        SQLiteStore._read_asset_proposal_projection_inputs
    )
    assert "SELECT * FROM contracts" in inspect.getsource(SQLiteStore.get_contract)
