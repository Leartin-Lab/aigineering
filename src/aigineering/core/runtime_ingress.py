"""RuntimeIngress — unified ingress for all runtime facts.

All production paths (Engine, CLI, server, method runtime, skill loader,
labels) must route assets, contracts, candidate submissions, and
replacement claims through this single ingress.  Direct store writes are
reserved for store implementations, transaction helpers, and explicit test
fixtures only.

Every accepted fact is signed, authority-checked, traced, and reduced.
The fact reducer projects asset consequences (activation readiness, output
satisfaction, contract completion, child cancellation).

References: W1 (Fact Ingress And Reactive Reducer) of
``.omo/plans/050-runtime-boundary-refactor-plan.md``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from aigineering.core.authority import RESERVED_PREFIXES
from aigineering.core.provenance import sign_asset
from aigineering.core.trace import create_entry

if TYPE_CHECKING:
    from aigineering.core.fact_reducer import FactReducer
    from aigineering.protocol.types import Asset, Candidate, Contract, ProjectionResult
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol

_logger = logging.getLogger(__name__)


def _is_protected_name(name: str) -> bool:
    """Return True when *name* starts with a protected prefix."""
    for prefix in RESERVED_PREFIXES:
        if name.startswith(prefix):
            return True
        # Also match the bare prefix form: "_sys_" should match "_sys"
        if prefix.endswith("_") and name == prefix.rstrip("_"):
            return True
    return False


class RuntimeIngress:
    """Single entry point for all runtime facts.

    Every asset, contract, and candidate submission passes through this
    ingress.  It handles signing, protected-name enforcement, store
    persistence, trace recording, and fact reduction — so callers never
    need to touch ``store.add_asset`` or ``store.add_contract`` directly.

    Parameters
    ----------
    store : StoreProtocol
        Asset and contract persistence.
    trace : TraceStoreProtocol
        Append-only trace store for audit records.
    fact_reducer : FactReducer or None
        Optional reducer that projects asset consequences into events.
    """

    def __init__(
        self,
        store: StoreProtocol,
        trace: TraceStoreProtocol,
        fact_reducer: FactReducer | None = None,
    ) -> None:
        self._store = store
        self._trace = trace
        self._reducer = fact_reducer

    # -- Asset acceptance ---------------------------------------------------

    def accept_asset(
        self,
        asset: Asset,
        *,
        source: str = "ingress",
        allow_protected: bool = False,
    ) -> Asset:
        """Accept an asset into the runtime.

        Signs the asset, enforces protected-name rules, persists it, and
        records a trace entry.  If a fact reducer is configured, it is
        called after commit.

        Parameters
        ----------
        asset : Asset
            The asset to accept (may be unsigned).
        source : str
            Ingress source label for trace attribution.
        allow_protected : bool
            Explicit override for protected-prefix names.

        Returns
        -------
        Asset
            The signed, persisted asset.

        Raises
        ------
        ValueError
            If *asset.name* uses a protected prefix and *allow_protected*
            is ``False``.
        """
        if _is_protected_name(asset.name) and not allow_protected:
            raise ValueError(
                f"Asset name '{asset.name}' uses a protected prefix. "
                f"Use allow_protected=True if intentional."
            )

        signed = sign_asset(asset)
        if not signed.signed_by:
            signed = sign_asset(asset, signed_by="engine")

        self._store.add_asset(signed)

        self._trace.append(
            create_entry(
                contract_id="runtime_ingress",
                event_type="asset_accepted",
                parent_id=signed.id,
                relation_target=signed.name,
                accepted_fragments=[
                    json.dumps(
                        {
                            "asset_id": signed.id,
                            "name": signed.name,
                            "origin": signed.origin,
                            "trust_tier": signed.trust_tier,
                            "source": source,
                        }
                    )
                ],
            )
        )

        if allow_protected and _is_protected_name(asset.name):
            self._trace.append(
                create_entry(
                    contract_id="runtime_ingress",
                    event_type="asset_accepted_protected_override",
                    parent_id=signed.id,
                    relation_target=signed.name,
                )
            )

        if self._reducer is not None:
            events = self._reducer.on_asset_created(signed)
            _logger.debug(
                "FactReducer produced %d events for asset %r", len(events), signed.name
            )
            self._apply_reducer_events(events, signed)

        return signed

    def _apply_reducer_events(
        self, events: list[object], asset: Asset
    ) -> None:
        """Apply FactReducer events: append trace entries for every
        detected consequence of the new asset.

        For ``contract_complete`` events the engine's completion state is
        also updated so that reactive parent completion actually works.
        """
        from aigineering.core.fact_reducer import FactReducerEvent

        for event in events:
            if not isinstance(event, FactReducerEvent):
                continue

            trace_event_type: str | None = None
            trace_kwargs: dict[str, object] = {"relation_target": event.asset_name}

            if event.type == "contract_complete":
                trace_event_type = "complete"
                if event.contract_id:
                    trace_kwargs["budget_remaining"] = 0
            elif event.type == "child_cancelled":
                trace_event_type = "cancelled"
                trace_kwargs["relation_type"] = "unreachable"
                trace_kwargs["rejected_fragments"] = [
                    "[unreachable] parent_complete: "
                    f"parent {event.details.get('parent_id', '?')} completed"
                ]
            elif event.type == "output_satisfied":
                trace_event_type = "output_satisfied"
            elif event.type == "activation_active":
                trace_event_type = "activation"
            elif event.type == "method_result_detected":
                trace_event_type = "method_result_detected"

            if trace_event_type is not None and event.contract_id:
                self._trace.append(
                    create_entry(
                        contract_id=event.contract_id,
                        event_type=trace_event_type,
                        parent_id=asset.id,
                        **trace_kwargs,
                    )
                )

    # -- Contract acceptance ------------------------------------------------

    def accept_contract(self, contract: Contract) -> Contract:
        """Accept a contract into the runtime.

        Validates that outputs do not use protected names (unless the
        contract has minting authority), persists the contract, and
        records a trace entry.

        Returns the persisted contract.

        Raises
        ------
        ValueError
            If an output uses a protected prefix and the contract does not
            have minting authority for that name.
        """
        for output_name in contract.outputs:
            if (
                _is_protected_name(output_name)
                and output_name not in contract.minting_authority
            ):
                raise ValueError(
                    f"Contract output '{output_name}' uses a protected prefix "
                    f"and is not in contract.minting_authority "
                    f"({list(contract.minting_authority)!r})."
                )

        self._store.add_contract(contract)

        self._trace.append(
            create_entry(
                contract_id="runtime_ingress",
                event_type="contract_accepted",
                parent_id=contract.id,
                relation_target=contract.id,
                accepted_fragments=[
                    json.dumps(
                        {
                            "contract_id": contract.id,
                            "name": contract.name,
                            "outputs": list(contract.outputs),
                            "budget": contract.budget,
                        }
                    )
                ],
            )
        )

        return contract

    # -- Candidate submission -----------------------------------------------

    def accept_candidate_submission(
        self,
        contract: Contract,
        candidate: Candidate,
        claim_id: str | None = None,
    ) -> ProjectionResult:
        """Project a worker candidate through the commitment boundary.

        Delegates to :func:`project_candidate` for authority and parse
        checks, then accepts each accepted asset through
        :meth:`accept_asset`.  Returns the final projection result with
        signed assets.

        Parameters
        ----------
        contract : Contract
            The contract the candidate was submitted for.
        candidate : Candidate
            The raw worker output to project.
        claim_id : str or None
            Optional claim identifier for idempotency tracking.

        Returns
        -------
        ProjectionResult
            The projection result with signed accepted assets.
        """
        from aigineering.core.projection import project_candidate
        from aigineering.protocol.types import ProjectionResult

        raw_result = project_candidate(contract, candidate)

        signed_assets: list[Asset] = []
        for asset in raw_result.accepted_assets:
            accepted = self.accept_asset(asset, source="candidate")
            signed_assets.append(accepted)

        rejected_dicts = [
            {
                "name": r.name,
                "content": r.content,
                "reject_reason": r.reject_reason,
                "category": r.category.value,
            }
            for r in raw_result.rejected_candidates
        ]

        self._trace.append(
            create_entry(
                contract_id=contract.id,
                event_type="candidate_submitted",
                worker_id=candidate.worker_id,
                candidate_raw=candidate.raw_output,
                accepted_fragments=[a.id for a in signed_assets],
                accepted_asset_names=[a.name for a in signed_assets],
                rejected_fragments=[
                    f"[{r['category']}] {r['name']}: {r['reject_reason']}"
                    for r in rejected_dicts
                ],
                authority_result=raw_result.status.value,
                authority_policy=(
                    json.dumps(dict(raw_result.authority_policy), sort_keys=True)
                    if raw_result.authority_policy is not None
                    else None
                ),
                usage_metadata=candidate.metadata,
            )
        )

        return ProjectionResult(
            accepted_assets=tuple(signed_assets),
            rejected_candidates=raw_result.rejected_candidates,
            raw_candidate=raw_result.raw_candidate,
            status=raw_result.status,
            authority_policy=raw_result.authority_policy,
        )
