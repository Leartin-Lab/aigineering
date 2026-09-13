"""Exact output selection for method evaluations."""

from types import SimpleNamespace

from aigineering.methods import evaluation
from aigineering.protocol.types import Asset, Contract


def _fixture(monkeypatch, *, outputs, extra_contracts=()):
    run = {
        "method_asset_id": "asset:method",
        "suite_asset_id": "asset:suite",
        "root_contract_id": "contract:root",
        "cases": [
            {
                "name": "case",
                "contract_id": "contract:case",
                "outputs": {"answer": "case.answer"},
            }
        ],
    }
    suite = {
        "schema": "method-cases-v1",
        "name": "suite",
        "cases": [{"name": "case", "expected": {"answer": "ok"}, "inputs": {}}],
    }
    package = SimpleNamespace(outputs={"answer": None})
    monkeypatch.setattr(
        evaluation, "inspect_run", lambda *args, **kwargs: (run, package)
    )
    assets = [
        Asset(id="asset:suite", name="suite", content=evaluation.canonical_json(suite))
    ]
    assets.extend(outputs)
    contracts = {
        "contract:root": Contract(id="contract:root"),
        "contract:case": Contract(id="contract:case", parent_id="contract:root"),
        "contract:sibling": Contract(id="contract:sibling"),
    }
    contracts.update({contract.id: contract for contract in extra_contracts})
    records = [
        (
            "record:root",
            SimpleNamespace(
                record_type="lifecycle.terminal",
                payload={"contract_id": "contract:root", "terminal": "complete"},
            ),
        ),
        (
            "record:case",
            SimpleNamespace(
                record_type="lifecycle.terminal",
                payload={"contract_id": "contract:case", "terminal": "complete"},
            ),
        ),
        (
            "record:producer",
            SimpleNamespace(
                record_type="lifecycle.terminal",
                payload={"contract_id": "contract:producer", "terminal": "complete"},
            ),
        ),
    ]
    result = evaluation.evaluate_run(
        "asset:run",
        get_asset=lambda asset_id: next(
            (asset for asset in assets if asset.id == asset_id), None
        ),
        get_contract=contracts.get,
        assets=assets,
        records=records,
    )
    return result


def test_same_output_name_in_another_lineage_is_ignored(monkeypatch):
    result = _fixture(
        monkeypatch,
        outputs=[
            Asset(
                id="asset:expected",
                name="case.answer",
                content="ok",
                created_by="contract:case",
            ),
            Asset(
                id="asset:sibling",
                name="case.answer",
                content="wrong",
                created_by="contract:sibling",
            ),
        ],
    )

    assert result["passed"] is True
    assert result["cases"][0]["outputs"] == {"answer": "asset:expected"}


def test_multiple_outputs_in_the_exact_lineage_fail_evaluation(monkeypatch):
    result = _fixture(
        monkeypatch,
        outputs=[
            Asset(
                id="asset:first",
                name="case.answer",
                content="ok",
                created_by="contract:case",
            ),
            Asset(
                id="asset:second",
                name="case.answer",
                content="ok",
                created_by="contract:case",
            ),
        ],
    )

    assert result["passed"] is False
    assert result["cases"][0]["outputs"] == {}
    assert (
        "expected exactly one lineage-bound output" in result["cases"][0]["reasons"][0]
    )


def test_output_from_deeper_descendant_is_selected(monkeypatch):
    result = _fixture(
        monkeypatch,
        outputs=[
            Asset(
                id="asset:descendant",
                name="case.answer",
                content="ok",
                created_by="contract:producer",
            )
        ],
        extra_contracts=(
            Contract(id="contract:intermediate", parent_id="contract:case"),
            Contract(id="contract:producer", parent_id="contract:intermediate"),
        ),
    )

    assert result["passed"] is True
    assert result["cases"][0]["outputs"] == {"answer": "asset:descendant"}


def test_unrelated_contract_cycle_does_not_match_or_loop(monkeypatch):
    result = _fixture(
        monkeypatch,
        outputs=[
            Asset(
                id="asset:cycle",
                name="case.answer",
                content="ok",
                created_by="contract:cycle-a",
            )
        ],
        extra_contracts=(
            Contract(id="contract:cycle-a", parent_id="contract:cycle-b"),
            Contract(id="contract:cycle-b", parent_id="contract:cycle-a"),
        ),
    )

    assert result["passed"] is False
    assert result["cases"][0]["outputs"] == {}
    assert (
        "expected exactly one lineage-bound output" in result["cases"][0]["reasons"][0]
    )
