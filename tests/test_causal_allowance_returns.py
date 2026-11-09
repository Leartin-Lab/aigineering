"""Causal allowance is reserved, bounded and never minted by raw writes."""

from __future__ import annotations

import pytest

from aigineering.core.causal_allowance import allowance_available
from aigineering.core.sqlite_store import SQLiteStore
from dataclasses import replace

from aigineering.core.ids import contract_identity_v3
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract
from conftest import candidate_runtime


def _records(store):
    return tuple(record for _, record in store.scan_runtime_records())


def test_child_publication_reserves_actual_allowance_and_refuses_overspend():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    root = runtime.accept_contract(Contract(id="", name="root", budget=5))
    child = Contract(id="", name="child", parent_id=root.id, origin="plan", budget=3)
    child = replace(child, id=contract_identity_v3(child))

    accepted = runtime.publisher.publish(
        (contract_declaration_effect(child),), idempotency_key="funded-child"
    )
    assert accepted.accepted, accepted
    child = accepted.contract
    assert child is not None
    assert allowance_available(root, _records(store)) == 2
    reservation = store.scan_runtime_records(record_type="allowance.reserved")
    assert len(reservation) == 1
    assert reservation[0][1].payload["amount"] == 3

    existing_contract_ids = {item.id for item in store.get_all_contracts()}
    too_large = Contract(
        id="", name="overspend", parent_id=root.id, origin="plan", budget=3
    )
    too_large = replace(too_large, id=contract_identity_v3(too_large))
    rejected = runtime.publisher.publish(
        (contract_declaration_effect(too_large),), idempotency_key="overspend"
    )
    assert not rejected.accepted
    assert {item.id for item in store.get_all_contracts()} == existing_contract_ids
    assert allowance_available(root, _records(store)) == 2
    store.close()


def test_raw_sqlite_append_cannot_mint_a_second_grant():
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    root = runtime.accept_contract(Contract(id="", name="root", budget=5))
    before = _records(store)
    forged = create_runtime_record(
        "allowance.granted",
        {
            "amount": 100,
            "contract_id": root.id,
            "grant_id": "grant:synthetic",
            "purpose": "root",
            "source_contract_id": "",
        },
    )

    with pytest.raises(ValueError, match="Candidate ingress commitment"):
        store.append_runtime_record(forged)

    assert _records(store) == before
    assert allowance_available(root, _records(store)) == 5
    store.close()
