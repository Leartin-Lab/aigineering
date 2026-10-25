from dataclasses import replace

from aigineering.core.causal_allowance import (
    allowance_available,
    fold_raw_allowance_records,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract


def test_raw_allowance_fold_matches_reference_grouped_semantics():
    contract = Contract(id="task:v3:parent", budget=50)
    records = (
        create_runtime_record(
            "allowance.granted", {"contract_id": contract.id, "amount": 20}
        ),
        create_runtime_record(
            "allowance.reserved", {"source_contract_id": contract.id, "amount": 8}
        ),
        create_runtime_record(
            "allowance.returned",
            {
                "source_contract_id": contract.id,
                "from_contract_id": contract.id,
                "amount": 3,
            },
        ),
        create_runtime_record(
            "allowance.extinguished", {"contract_id": contract.id, "amount": 2}
        ),
    )

    folded = fold_raw_allowance_records(contract.id, records)

    assert folded is not None
    assert folded.grant_count == 1
    assert folded.granted_total == 20
    assert folded.reserved_total == 8
    assert folded.returned_in_total == 3
    assert folded.returned_out_total == 3
    assert folded.extinguished_total == 2
    assert max(
        0,
        folded.granted_total
        - folded.reserved_total
        + folded.returned_in_total
        - folded.returned_out_total
        - folded.extinguished_total,
    ) == allowance_available(contract, records)


def test_raw_allowance_fold_uses_last_record_for_duplicate_id():
    contract = Contract(id="task:v3:parent", budget=50)
    first = create_runtime_record(
        "allowance.granted", {"contract_id": contract.id, "amount": 30}
    )
    replaced = replace(
        create_runtime_record(
            "allowance.granted", {"contract_id": contract.id, "amount": 40}
        ),
        id=first.id,
    )

    folded = fold_raw_allowance_records(contract.id, (first, replaced))

    assert folded is not None
    assert folded.grant_count == 1
    assert folded.granted_total == 40


def test_raw_allowance_fold_marks_unconvertible_amount_opaque():
    contract = Contract(id="task:v3:parent", budget=50)
    bad = create_runtime_record(
        "allowance.granted", {"contract_id": contract.id, "amount": None}
    )

    assert fold_raw_allowance_records(contract.id, (bad,)) is None


def test_raw_allowance_fold_ignores_unrelated_bad_amount_and_keeps_signed_values():
    contract = Contract(id="task:v3:parent", budget=50)
    unrelated = create_runtime_record(
        "allowance.granted", {"contract_id": "other", "amount": None}
    )
    grant = create_runtime_record(
        "allowance.granted", {"contract_id": contract.id, "amount": -2}
    )
    zero_grant = create_runtime_record(
        "allowance.granted", {"contract_id": contract.id, "amount": 0}
    )
    folded = fold_raw_allowance_records(contract.id, (unrelated, grant, zero_grant))

    assert folded is not None
    assert folded.grant_count == 2
    assert folded.granted_total == -2
    assert allowance_available(contract, (unrelated, grant, zero_grant)) == 0


def test_raw_allowance_fold_matches_reference_with_distinct_return_donor():
    source = Contract(id="source", budget=20)
    donor = "donor"
    returned = create_runtime_record(
        "allowance.returned",
        {"source_contract_id": source.id, "from_contract_id": donor, "amount": 4},
    )
    source_totals = fold_raw_allowance_records(source.id, (returned,))
    donor_totals = fold_raw_allowance_records(donor, (returned,))

    assert source_totals is not None and donor_totals is not None
    assert source_totals.returned_in_total == 4
    assert source_totals.returned_out_total == 0
    assert donor_totals.returned_in_total == 0
    assert donor_totals.returned_out_total == 4


def test_raw_allowance_fold_keeps_integer_coercion_and_budget_fallback_separate():
    contract = Contract(id="task:v3:parent", budget=11)
    empty = fold_raw_allowance_records(contract.id, ())
    assert empty is not None and empty.grant_count == 0
    assert allowance_available(contract, ()) == 11

    for index, amount in enumerate(("3", True, 2.8, -4, 0)):
        record = create_runtime_record(
            "allowance.granted",
            {"contract_id": contract.id, "amount": amount},
            recorded_at=f"2026-01-01T00:00:{index:02d}Z",
        )
        folded = fold_raw_allowance_records(contract.id, (record,))
        assert folded is not None
        assert folded.grant_count == 1
        assert folded.granted_total == int(amount)
        assert allowance_available(contract, (record,)) == max(0, int(amount))
