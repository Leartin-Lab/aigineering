"""The bounded declaration reader stays an optional Store adapter concern."""

from aigineering.core.store import CandidateCommitStore, StoreProtocol


def test_projection_reader_is_optional_and_not_a_required_store_protocol_member():
    assert "read_contract_declaration_projection_inputs" not in StoreProtocol.__dict__
    assert "read_contract_declaration_projection_inputs" not in (
        CandidateCommitStore.__dict__
    )


def test_effect_projectors_do_not_query_the_store_for_projection_inputs():
    import inspect

    from aigineering.core import effect_projectors

    source = inspect.getsource(effect_projectors)
    assert "read_contract_declaration_projection_inputs" not in source
    assert "get_all_contracts(" not in source
    assert "scan_runtime_records(" not in source
