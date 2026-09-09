"""Reconstructable method test plans and exact-result evaluation.

Test execution remains ordinary Worker work. These functions only construct or
inspect values; callers publish proposals through the signed Candidate boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import re

from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.protocol.identity import canonical_json, contract_from_fields
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import contract_to_dict
from aigineering.methods.packages import (
    build_invocation,
    resolve_package,
    visible_asset,
)
from aigineering.methods.schema import parse_cases, validate_cases

EVALUATION_POLICY = "method-evaluation-v1"


def json_asset(name: str, payload: Mapping) -> Asset:
    return build_control_plane_asset(
        name=name,
        content=canonical_json(payload),
        content_type="application/json",
        origin="method-adapter",
        trust_tier="untrusted",
    )


def _json_value(text: str):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate method record member")
            value[key] = item
        return value

    try:
        result = json.loads(text, object_pairs_hook=unique)
        canonical_json(result)
    except RecursionError as exc:
        raise ValueError("method JSON nesting is too deep") from exc
    return result


def read_method_record(asset: Asset, schema: str) -> dict:
    if asset.disclosure_view != "original" or asset.tombstoned or not asset.promptable:
        raise ValueError("method record is unavailable for disclosure")
    result = _json_value(asset.content)
    if not isinstance(result, dict) or result.get("schema") != schema:
        raise ValueError(f"expected {schema}")
    canonical_json(result)
    return result


def compile_test_contracts(
    method_asset_id: str,
    suite_asset_id: str,
    *,
    name: str,
    budget: int,
    case_input_ids: Sequence[Mapping[str, str]],
    get_asset: Callable,
    allowed_tools: tuple[str, ...] = (),
) -> tuple[Contract, tuple[Contract, ...], dict]:
    if not isinstance(name, str) or not re.fullmatch(
        r"[a-zA-Z][a-zA-Z0-9_.-]{0,47}", name
    ):
        raise ValueError(
            "test run name must be an ordinary identifier of at most 48 characters"
        )
    package, closure = resolve_package(method_asset_id, get_asset)
    suite_asset = visible_asset(suite_asset_id, get_asset)
    suite = parse_cases(suite_asset.content)
    validate_cases(suite, package)
    if suite_asset_id in {asset.id for asset in closure}:
        raise ValueError("test answers must not be in method context")
    if len(case_input_ids) != len(suite["cases"]):
        raise ValueError("case input bindings do not match test suite")
    children = []
    case_rows = []
    for index, (case, inputs) in enumerate(
        zip(suite["cases"], case_input_ids, strict=True)
    ):
        if set(inputs) != set(case["inputs"]):
            raise ValueError("case input slots do not match")
        for slot, asset_id in inputs.items():
            if visible_asset(asset_id, get_asset).content != case["inputs"][slot]:
                raise ValueError(
                    "case input content differs from the frozen test suite"
                )
        outputs = {slot: f"{name}.case{index}.{slot}" for slot in package.outputs}
        child = build_invocation(
            method_asset_id,
            inputs=inputs,
            outputs=outputs,
            get_asset=get_asset,
            name=f"{name}.case{index}",
            budget=budget,
            allowed_tools=allowed_tools,
            provisional=True,
        )
        children.append(child)
        case_rows.append(
            {"name": case["name"], "inputs": dict(inputs), "outputs": outputs}
        )
    contexts = tuple(
        sorted({value for child in children for value in child.context_asset_ids})
    )
    labels = tuple(sorted({label for child in children for label in child.labels}))
    required = package.requirements
    root = build_control_plane_contract(
        name=name,
        description="Method evaluation suite: ordinary independently claimable cases.",
        outputs=tuple(output for child in children for output in child.outputs),
        budget=budget * len(children),
        labels=labels,
        context_asset_ids=contexts,
        tool_scope=tuple(required["tool_scope"]),
        worker_capabilities=("method.test-coordinator",),
        delegation_capabilities=tuple(
            sorted(
                set(required["worker_capabilities"])
                | set(required["delegation_capabilities"])
            )
        ),
        delegation_pools=tuple(
            sorted(set(required["worker_pools"]) | set(required["delegation_pools"]))
        ),
    )
    declared_children = []
    for child, row in zip(children, case_rows, strict=True):
        fields = contract_to_dict(child)
        fields.pop("id")
        fields.update(parent_id=root.id, origin="plan")
        declared = contract_from_fields(**fields)
        row["contract_id"] = declared.id
        declared_children.append(declared)
    manifest = {
        "schema": "method-test-run-v1",
        "name": name,
        "method_asset_id": method_asset_id,
        "suite_asset_id": suite_asset_id,
        "budget_per_case": budget,
        "root_contract_id": root.id,
        "cases": case_rows,
    }
    return root, tuple(declared_children), manifest


def inspect_run(run_asset_id: str, *, get_asset: Callable, get_contract: Callable):
    run = read_method_record(
        visible_asset(run_asset_id, get_asset), "method-test-run-v1"
    )
    try:
        package, _ = resolve_package(run["method_asset_id"], get_asset)
        root, children, expected = compile_test_contracts(
            run["method_asset_id"],
            run["suite_asset_id"],
            name=run["name"],
            budget=run["budget_per_case"],
            case_input_ids=[case["inputs"] for case in run["cases"]],
            get_asset=get_asset,
            allowed_tools=tuple(package.requirements["tool_scope"]),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed method test run") from exc
    if run != expected:
        raise ValueError("method run does not match its immutable test specification")
    for contract in (root, *children):
        if get_contract(contract.id) != contract:
            raise ValueError("method test Contract does not match its exact invocation")
    return run, package


def evaluate_run(
    run_asset_id: str,
    *,
    get_asset: Callable,
    get_contract: Callable,
    assets: Sequence[Asset],
    records: Sequence,
) -> dict:
    run, package = inspect_run(
        run_asset_id, get_asset=get_asset, get_contract=get_contract
    )
    suite = parse_cases(visible_asset(run["suite_asset_id"], get_asset).content)
    terminals = {
        str(record.payload["contract_id"]): str(record.payload["terminal"])
        for _, record in records
        if record.record_type == "lifecycle.terminal"
    }

    def descendant(producer: str, ancestor: str) -> bool:
        seen = set()
        while producer and producer not in seen:
            if producer == ancestor:
                return True
            seen.add(producer)
            contract = get_contract(producer)
            producer = contract.parent_id if contract is not None else ""
        return False

    results = []
    for row, case in zip(run["cases"], suite["cases"], strict=True):
        contract_id = row["contract_id"]
        # A plan may expand into descendants. Root closure bounds observation;
        # sibling replacements do not prove this exact case execution.
        if run["root_contract_id"] not in terminals:
            raise ValueError("method test run has not reached a terminal outcome")
        selected = {}
        passed = terminals[run["root_contract_id"]] == "complete"
        reasons = [] if passed else ["test root did not complete successfully"]
        for slot, output_name in row["outputs"].items():
            matches = [
                asset
                for asset in assets
                if asset.name == output_name
                and descendant(asset.created_by, contract_id)
            ]
            if len(matches) != 1:
                passed = False
                reasons.append(f"{slot}: expected exactly one lineage-bound output")
                continue
            asset = visible_asset(matches[0].id, get_asset)
            if terminals.get(asset.created_by) != "complete":
                passed = False
                reasons.append(f"{slot}: producer did not complete successfully")
            selected[slot] = asset.id
            try:
                actual = (
                    asset.content
                    if package.outputs[slot] is None
                    else _json_value(asset.content)
                )
                equal = canonical_json(actual) == canonical_json(
                    deep_thaw(case["expected"][slot])
                )
            except (ValueError, TypeError):
                equal = False
            if not equal:
                passed = False
                reasons.append(f"{slot}: output differs from expected result")
        results.append(
            {
                "name": row["name"],
                "contract_id": contract_id,
                "outputs": selected,
                "passed": passed,
                "reasons": reasons,
            }
        )
    return {
        "schema": EVALUATION_POLICY,
        "run_asset_id": run_asset_id,
        "method_asset_id": run["method_asset_id"],
        "suite_asset_id": run["suite_asset_id"],
        "root_contract_id": run["root_contract_id"],
        "cases": results,
        "passed": all(row["passed"] for row in results),
    }


def assessment_contract(
    run_asset_id: str, report: Mapping, *, get_asset: Callable
) -> Contract:
    ids = {run_asset_id, report["method_asset_id"], report["suite_asset_id"]}
    ids.update(value for row in report["cases"] for value in row["outputs"].values())
    assets = [visible_asset(value, get_asset) for value in sorted(ids)]
    suffix = run_asset_id.rsplit(":", 1)[-1][:20]
    return build_control_plane_contract(
        name=f"method.assess.{suffix}",
        outputs=(f"method.assessment.{suffix}",),
        description="Publish the exact method test evaluation; an independent method verifier must review its evidence.",
        budget=1,
        labels=tuple(sorted({a.name for a in assets})),
        context_asset_ids=tuple(sorted(ids)),
        worker_capabilities=("method.evaluate",),
        acceptance_policy={
            "mode": "independent",
            "policy_version": EVALUATION_POLICY,
            "required_attestations": 1,
            "verifier_capabilities": ["method.verify"],
            "evidence_asset_ids": sorted(ids),
        },
    )


def require_evaluation(
    method_asset_id: str,
    evaluation_asset_id: str,
    *,
    get_asset: Callable,
    get_contract: Callable,
    assets: Sequence[Asset],
    records: Sequence,
) -> dict:
    evaluation = visible_asset(evaluation_asset_id, get_asset)
    report = read_method_record(evaluation, EVALUATION_POLICY)
    if (
        report.get("method_asset_id") != method_asset_id
        or report.get("passed") is not True
    ):
        raise ValueError(
            "method requires passing evaluation for this exact package Asset"
        )
    expected = evaluate_run(
        report.get("run_asset_id", ""),
        get_asset=get_asset,
        get_contract=get_contract,
        assets=assets,
        records=records,
    )
    if report != expected:
        raise ValueError("method evaluation does not match committed test outcomes")
    contract = assessment_contract(report["run_asset_id"], report, get_asset=get_asset)
    if evaluation.created_by != contract.id or get_contract(contract.id) != contract:
        raise ValueError(
            "method evaluation is not bound to its required acceptance policy"
        )
    if not any(
        record.record_type == "output.qualified"
        and record.payload.get("asset_id") == evaluation_asset_id
        and record.payload.get("contract_id") == contract.id
        for _, record in records
    ):
        raise ValueError("method evaluation lacks independent qualification")
    return report
