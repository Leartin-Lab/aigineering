"""aig verify and aig readiness commands."""

from __future__ import annotations

import click

from aigineering.cli._common import _output_json, _persistent_store
from aigineering.cli._candidate import require_accepted
from aigineering.core.sufficiency import check_sufficiency
from aigineering.core.ids import acceptance_policy_id
from aigineering.core.verification import (
    batch_verify_definition,
    verify_replacement_claims,
)
from aigineering.protocol.effect_builders import asset_attestation_effect
from aigineering.local_identity import ensure_local_plugin_publisher


@click.group("verify")
def verify() -> None:
    """Verify content hashes and replacement claims."""


@verify.command("attest")
@click.option("--contract", "contract_id", required=True, help="Contract ID.")
@click.option("--output", "output_name", required=True, help="Declared output name.")
@click.option("--asset", "asset_id", required=True, help="Exact Asset ID to attest.")
@click.option(
    "--verdict",
    type=click.Choice(("accepted", "rejected")),
    default="accepted",
    show_default=True,
)
@click.option(
    "--evidence-asset",
    "evidence_asset_ids",
    multiple=True,
    help="Evidence Asset ID (repeatable).",
)
@click.option("--json", "json_output", is_flag=True)
def verify_attest(
    contract_id: str,
    output_name: str,
    asset_id: str,
    verdict: str,
    evidence_asset_ids: tuple[str, ...],
    json_output: bool,
) -> None:
    """Publish a signed independent attestation for one exact output Asset."""
    store = _persistent_store()
    try:
        publisher = ensure_local_plugin_publisher(
            store,
            "human.verify.v1",
            ("asset.attest", "verify.human"),
        )
        contract = store.get_contract(contract_id)
        if contract is None:
            raise LookupError(f"unknown Contract {contract_id!r}")
        policy = contract.acceptance_policy
        if policy is None:
            raise ValueError("Contract has no acceptance policy")
        policy_version = str(policy.get("policy_version", ""))
        rubric_asset_ids = tuple(policy.get("rubric_asset_ids", ()))
        required_evidence_ids = tuple(policy.get("evidence_asset_ids", ()))
        if evidence_asset_ids != required_evidence_ids:
            raise ValueError(
                "--evidence-asset values must exactly match the Contract policy"
            )
        decision = require_accepted(
            publisher.publish(
                (
                    asset_attestation_effect(
                        contract_id,
                        output_name,
                        asset_id,
                        policy_id=acceptance_policy_id(policy),
                        policy_version=policy_version,
                        verdict=verdict,
                        rubric_asset_ids=rubric_asset_ids,
                        evidence_asset_ids=evidence_asset_ids,
                    ),
                ),
                idempotency_key=(
                    f"attest:{contract_id}:{output_name}:{asset_id}:{verdict}:"
                    f"{','.join(evidence_asset_ids)}"
                ),
                causal_parents=(asset_id, *evidence_asset_ids),
            )
        )
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {
        "accepted": decision.accepted,
        "asset_id": asset_id,
        "contract_id": contract_id,
        "output_name": output_name,
        "qualified": any(
            record.record_type == "output.qualified"
            for record in decision.runtime_records
        ),
        "verdict": verdict,
    }
    if json_output:
        _output_json(payload)
    else:
        click.echo(
            f"Attestation committed: {verdict} {contract_id}/{output_name} -> {asset_id}"
        )


@verify.command("hash")
@click.option(
    "--definition-hash",
    "def_hash",
    required=True,
    help="Definition hash (def:<hex>) to verify content hashes for.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def verify_hash(def_hash: str, json_output: bool) -> None:
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


@verify.command("replacements")
@click.option(
    "--definition-hash",
    "def_hash",
    default=None,
    help="Filter claims by definition hash.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON instead of human-readable text.",
)
def verify_replacements(def_hash: str | None, json_output: bool) -> None:
    """Verify all replacement claims in the store."""
    store = _persistent_store()

    if def_hash:
        claims = store.get_claims_by_definition(def_hash)
    else:
        claims = []
        seen: set[str] = set()
        for asset in store.get_all_assets():
            for c in store.get_claims_by_definition(asset.definition_hash):
                if c.id not in seen:
                    claims.append(c)
                    seen.add(c.id)
            for c in store.get_claims_for_asset(asset.id):
                if c.id not in seen:
                    claims.append(c)
                    seen.add(c.id)

    if not claims:
        if json_output:
            _output_json({"pass_count": 0, "fail_count": 0, "results": []})
        else:
            click.echo("No replacement claims found in store.")
        return

    result = verify_replacement_claims(store, claims)

    if json_output:
        _output_json(result)
        return

    click.echo(
        f"Replacement claims: {result['pass_count']} pass, {result['fail_count']} fail"
    )
    for r in result["results"]:
        status = "✓" if r["valid"] else "✗"
        click.echo(f"  {status} {r['claim_id'][:32]}... ({r['claim_type']})")
        for issue in r.get("issues", []):
            click.echo(f"    - {issue}")


@click.command("readiness")
@click.option(
    "--contract",
    "contract_id",
    required=True,
    help="Contract ID to check readiness for",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
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
        click.echo("  Version conflicts:")
        for vc in report["version_conflicts"]:
            click.echo(f"    def_hash={vc['definition_hash']} names={vc['names']}")
    if report["trust_gaps"]:
        click.echo(f"  Trust gaps:      {report['trust_gaps']}")
    if report["seal_gaps"]:
        click.echo(f"  Seal gaps:  {report['seal_gaps']}")
