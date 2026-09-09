"""Local method application services with an injected signed publisher."""

from __future__ import annotations

from collections.abc import Mapping
import json

from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.local_identity import ensure_local_worker_host
from aigineering.methods.evaluation import (
    EVALUATION_POLICY,
    read_method_record,
    assessment_contract,
    compile_test_contracts,
    evaluate_run,
    json_asset,
    require_evaluation,
)
from aigineering.methods.packages import (
    build_invocation,
    package_proposal,
    resolve_package,
    visible_asset,
)
from aigineering.methods.schema import (
    canonical_package,
    package_to_dict,
    parse_cases,
    parse_package,
    validate_cases,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
    asset_attestation_effect,
)
from aigineering.protocol.identity import (
    acceptance_policy_id,
    canonical_json,
    compute_content_hash,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Candidate
from aigineering.runtime import claim_next_package, execute_claimed_package


class _AssessmentWorker:
    worker_id = "worker:method-evaluator-v1"

    def __init__(self, report: Mapping, contract_id: str):
        self.report = canonical_json(report)
        self.contract_id = contract_id

    def registration(self):
        return WorkerRegistration(self.worker_id, capabilities=("method.evaluate",))

    def invoke(self, contract, disclosed_assets):
        if contract.id != self.contract_id:
            raise ValueError("method evaluator received a different Contract")
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps({"outputs": {contract.outputs[0]: self.report}}),
        )


class MethodService:
    """Orchestrate explicit adapter operations without private execution state.

    ``publish`` signs ordinary effects with the caller's selected authority.
    The Store remains the source of every task, report and eligibility decision.
    """

    def __init__(self, store, publish):
        self.store = store
        self.publish = publish

    def _commit(self, *, assets=(), contracts=()):
        effects = tuple(contract_declaration_effect(c) for c in contracts) + tuple(
            asset_proposal_effect(a) for a in assets
        )
        identity = compute_content_hash(
            canonical_json(
                [
                    {"type": e.effect_type, "payload": deep_thaw(e.payload)}
                    for e in effects
                ]
            )
        )
        decision = self.publish(effects, idempotency_key=f"method:{identity}")
        if not decision.accepted:
            reason = next(
                (
                    str(r.payload.get("reason", "method Candidate rejected"))
                    for r in decision.runtime_records
                    if r.record_type.endswith("rejected")
                ),
                "method Candidate rejected",
            )
            raise ValueError(reason)
        return decision

    def _asset(self, proposal):
        decision = self._commit(assets=(proposal,))
        if len(decision.assets) != 1:
            raise ValueError("method publication did not produce exactly one Asset")
        return decision.assets[0]

    def import_package(self, text: str, name: str | None = None):
        package = parse_package(text)
        proposal = package_proposal(package, name=name)
        resolve_package(
            proposal.id,
            lambda asset_id: (
                proposal if asset_id == proposal.id else self.store.get_asset(asset_id)
            ),
        )
        return self._asset(proposal)

    def import_cases(self, text: str, name: str):
        return self._asset(json_asset(name, deep_thaw(parse_cases(text))))

    def instantiate(
        self,
        method_id,
        *,
        inputs,
        outputs,
        name,
        budget,
        allowed_tools=(),
        evaluation_id=None,
        allow_unverified=False,
        extra_context=(),
    ):
        if evaluation_id is not None:
            require_evaluation(
                method_id,
                evaluation_id,
                get_asset=self.store.get_asset,
                get_contract=self.store.get_contract,
                assets=self.store.get_all_assets(),
                records=self.store.scan_runtime_records(),
            )
        elif not allow_unverified:
            raise ValueError(
                "select an independently qualified evaluation or explicitly allow an unverified trial"
            )
        contract = build_invocation(
            method_id,
            inputs=inputs,
            outputs=outputs,
            name=name,
            budget=budget,
            allowed_tools=allowed_tools,
            get_asset=self.store.get_asset,
            evaluation_asset_id=evaluation_id,
            provisional=evaluation_id is None,
            extra_context=extra_context,
        )
        self._commit(contracts=(contract,))
        return contract

    def create(self, request_id, *, output, name, budget, context=()):
        author = self.import_package(canonical_package(authoring_package()))
        return self.instantiate(
            author.id,
            inputs={"request": request_id},
            outputs={"package": output},
            name=name,
            budget=budget,
            allow_unverified=True,
            extra_context=context,
        )

    def prepare_tests(self, method_id, suite_id, *, name, budget, allowed_tools=()):
        package, _ = resolve_package(method_id, self.store.get_asset)
        suite = parse_cases(visible_asset(suite_id, self.store.get_asset).content)
        validate_cases(suite, package)
        case_input_ids = []
        for index, case in enumerate(suite["cases"]):
            bindings = {}
            for slot, content in case["inputs"].items():
                asset = self._asset(
                    build_control_plane_asset(
                        name=f"{name}.input{index}.{slot}",
                        content=content,
                        origin="method-test",
                        trust_tier="untrusted",
                    )
                )
                bindings[slot] = asset.id
            case_input_ids.append(bindings)
        root, children, manifest = compile_test_contracts(
            method_id,
            suite_id,
            name=name,
            budget=budget,
            case_input_ids=case_input_ids,
            get_asset=self.store.get_asset,
            allowed_tools=allowed_tools,
        )
        self._commit(contracts=(root, *children))
        run = self._asset(json_asset(f"{name}.run", manifest))
        return run, root

    def assess(self, run_id):
        report = evaluate_run(
            run_id,
            get_asset=self.store.get_asset,
            get_contract=self.store.get_contract,
            assets=self.store.get_all_assets(),
            records=self.store.scan_runtime_records(),
        )
        contract = assessment_contract(run_id, report, get_asset=self.store.get_asset)
        self._commit(contracts=(contract,))
        existing = [
            a
            for a in self.store.get_assets_by_name(contract.outputs[0])
            if a.created_by == contract.id
        ]
        if existing:
            if len(existing) != 1 or json.loads(existing[0].content) != report:
                raise ValueError("existing assessment differs; publish a new test run")
            return existing[0], contract
        worker = _AssessmentWorker(report, contract.id)
        host = ensure_local_worker_host(self.store, worker)
        claimed = claim_next_package(
            self.store, worker_id=host.worker_id, contract_id=contract.id
        )
        if claimed is None:
            raise ValueError("method assessment is not claimable")
        execute_claimed_package(claimed, host, self.store)
        results = [
            a
            for a in self.store.get_assets_by_name(contract.outputs[0])
            if a.created_by == contract.id
        ]
        if len(results) != 1:
            raise ValueError("method assessment did not publish its declared result")
        return results[0], contract

    def attestation_effect(self, evaluation_id, verdict="accepted"):
        if verdict not in {"accepted", "rejected"}:
            raise ValueError("unknown method evaluation verdict")
        asset = visible_asset(evaluation_id, self.store.get_asset)
        report = read_method_record(asset, EVALUATION_POLICY)
        expected = evaluate_run(
            report.get("run_asset_id", ""),
            get_asset=self.store.get_asset,
            get_contract=self.store.get_contract,
            assets=self.store.get_all_assets(),
            records=self.store.scan_runtime_records(),
        )
        if report != expected:
            raise ValueError(
                "method evaluation differs from committed execution evidence"
            )
        if verdict == "accepted" and not report["passed"]:
            raise ValueError("a failed evaluation cannot authorize method reuse")
        contract = assessment_contract(
            report["run_asset_id"], report, get_asset=self.store.get_asset
        )
        if (
            asset.created_by != contract.id
            or self.store.get_contract(contract.id) != contract
        ):
            raise ValueError(
                "evaluation lacks its exact independent acceptance Contract"
            )
        policy = contract.acceptance_policy
        return asset_attestation_effect(
            contract_id=contract.id,
            output_name=asset.name,
            asset_id=asset.id,
            verdict=verdict,
            policy_id=acceptance_policy_id(policy),
            policy_version=EVALUATION_POLICY,
            evidence_asset_ids=tuple(policy["evidence_asset_ids"]),
        )

    def inspect(self, method_id):
        package, closure = resolve_package(method_id, self.store.get_asset)
        return {
            "asset_id": method_id,
            "package": package_to_dict(package),
            "context_asset_ids": [a.id for a in closure],
            "status": "published",
            "note": "Publication does not grant execution authority or imply independent evaluation.",
        }


def authoring_package():
    """A seed method that produces the same package format as any other Worker.

    It has no tools, special execution permissions or private continuation loop.
    Its output remains unverified until separately tested and accepted.
    """
    template = {
        "schema": "method-package-v1",
        "name": "new-method",
        "version": "1.0.0",
        "description": "Describe the specific problem and applicability.",
        "instructions": "Describe the procedure, uncertainty handling and declared outputs.",
        "inputs": ["source"],
        "outputs": {
            "assessment": {"verdict": "nonempty_string", "reason": "nonempty_string"}
        },
        "requirements": {
            field: []
            for field in (
                "tool_scope",
                "worker_capabilities",
                "worker_pools",
                "delegation_capabilities",
                "delegation_pools",
            )
        },
        "dependencies": [],
        "context_asset_ids": [],
        "examples": [],
    }
    instructions = (
        "Create or revise a bounded declarative method for the request. Use any explicitly "
        "disclosed prior package and feedback as evidence. Return one complete JSON package "
        "as the text of the bound package output via /exec. Do not execute generated code, "
        "grant authority, mark the package verified, or invent dependency Asset IDs. "
        "Use only declared inputs; describe unsupported and uncertain cases. Include examples "
        "with name, inputs (slot to text) and expected (output slot to JSON result). "
        "Examples must match the declared slots; they are not independent held-out tests. "
        "Shape types are string, nonempty_string, number, boolean, exact-key objects or "
        "single-item nonempty array shapes; null means an unconstrained text output. "
        "No floats or unsafe JSON integers; at most 32 inputs, outputs and examples. "
        "Use the exact top-level fields in this template:\n" + canonical_json(template)
    )
    template.update(
        name="method-author",
        description="Create a candidate method package from an explicit problem and optional feedback.",
        instructions=instructions,
        inputs=["request"],
        outputs={"package": None},
    )
    return parse_package(canonical_json(template))
