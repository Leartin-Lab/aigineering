"""aig verify and aig readiness commands."""

from __future__ import annotations

import click

from aigineering.cli._common import _output_json, _persistent_store
from aigineering.core.sufficiency import check_sufficiency
from aigineering.core.verification import batch_verify_definition


@click.command("verify")
@click.option(
    "--definition-hash", "def_hash", required=True,
    help="Definition hash (def:<hex>) to verify content hashes for.",
)
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def verify(def_hash: str, json_output: bool) -> None:
    """Batch-verify content hashes for all assets under a definition hash."""
    store = _persistent_store()
    result = batch_verify_definition(store, def_hash)

    if json_output:
        _output_json(result)
        return

    click.echo(f"Definition: {def_hash}")
    click.echo(f"  Pass: {result['pass_count']}  Fail: {result['fail_count']}")
    for r in result["results"]:
        status = "✓" if r["valid"] else "✗"
        click.echo(f"  {status} {r['asset_id']}")
        if not r["valid"]:
            if r.get("expected_content_hash") != r.get("content_hash"):
                click.echo(
                    f"    content_hash mismatch: "
                    f"stored={r['content_hash']} expected={r['expected_content_hash']}"
                )
            if r.get("expected_definition_hash") != r.get("definition_hash"):
                click.echo(
                    f"    definition_hash mismatch: "
                    f"stored={r['definition_hash']} expected={r['expected_definition_hash']}"
                )


@click.command("readiness")
@click.option("--contract", "contract_id", required=True, help="Contract ID to check readiness for")
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def readiness(
    contract_id: str,
    json_output: bool,
) -> None:
    """Check contract readiness and produce a sufficiency report."""
    store = _persistent_store()
    contract = store.get_contract(contract_id)
    if contract is None:
        if json_output:
            _output_json({"error": f"Contract '{contract_id}' not found."})
        else:
            click.echo(f"Contract '{contract_id}' not found.")
        return

    report = check_sufficiency(contract, store)

    if json_output:
        _output_json(report)
        return

    click.echo(f"Readiness report for contract '{contract.name}' ({contract.id}):")
    click.echo(f"  Recommendation: {report['recommendation']}")
    click.echo(f"  Sufficient:      {report['sufficiency_ok']}")
    if report["missing_inputs"]:
        click.echo(f"  Missing inputs:  {report['missing_inputs']}")
    if report["stale_assets"]:
        click.echo(f"  Stale assets:    {report['stale_assets']}")
    if report["version_conflicts"]:
        click.echo(f"  Version conflicts:")
        for vc in report["version_conflicts"]:
            click.echo(f"    def_hash={vc['definition_hash']} names={vc['names']}")
    if report["trust_gaps"]:
        click.echo(f"  Trust gaps:      {report['trust_gaps']}")
    if report["seal_gaps"]:
        click.echo(f"  Seal gaps:  {report['seal_gaps']}")
