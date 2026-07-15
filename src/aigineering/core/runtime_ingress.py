"""Legacy compatibility ingress for in-process Engine and Method code.

External publishers and claim-bound worker submissions no longer depend on
this adapter. It remains temporarily for the unshipped Engine and Method
migration tests; CandidateCommitter and ``submit_candidate`` share the same
pure Asset-fact reduction function directly.

Every accepted fact is signed, authority-checked, traced, and reduced.
The fact reducer projects asset consequences (activation readiness, output
satisfaction, contract completion, child cancellation).

References: W1 (Fact Ingress And Reactive Reducer) of
``.omo/plans/050-runtime-boundary-refactor-plan.md``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from aigineering.core.authority import (
    ReservedNamespaceError,
    matched_reserved_prefix,
)
from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.provenance import sign_asset
from aigineering.core.trace import create_entry
from aigineering.core.fact_materialization import (
    asset_committed_record,
    reduce_asset_facts,
    trace_records,
)
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.wire import contract_to_dict

if TYPE_CHECKING:
    from aigineering.core.fact_reducer import FactReducer
    from aigineering.protocol.types import (
        Asset,
        Contract,
        ReplacementClaim,
    )
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol

_logger = logging.getLogger(__name__)


def _is_protected_name(name: str) -> bool:
    return matched_reserved_prefix(name) is not None


def _get_matched_prefix(name: str) -> str | None:
    return matched_reserved_prefix(name)


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
        from aigineering.core.store import require_runtime_store
        from aigineering.core.fact_reducer import FactReducer

        self._store = require_runtime_store(store)
        self._trace = trace
        self._reducer = fact_reducer or FactReducer(self._store, trace)

    def reduce_assets(
        self, assets: Sequence[Asset]
    ) -> tuple[list[object], tuple[RuntimeRecord, ...]]:
        """Return the canonical consequences of one atomic Asset batch."""
        return reduce_asset_facts(
            self._store, self._trace, assets, reducer=self._reducer
        )

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
        ReservedNamespaceError
            If *asset.name* uses a protected prefix and *allow_protected*
            is ``False``.
        """
        if not allow_protected:
            prefix = _get_matched_prefix(asset.name)
            if prefix is not None:
                raise ReservedNamespaceError(asset.name, prefix)

        signed = sign_asset(asset)
        if not signed.signed_by:
            signed = sign_asset(asset, signed_by="engine")
        asset_record = asset_committed_record(signed)

        main_entry = create_entry(
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

        protected_entry: object | None = None
        if allow_protected and _is_protected_name(asset.name):
            protected_entry = create_entry(
                contract_id="runtime_ingress",
                event_type="asset_accepted_protected_override",
                parent_id=signed.id,
                relation_target=signed.name,
            )

        trace_entries: list[object] = [main_entry]
        if protected_entry is not None:
            trace_entries.append(protected_entry)
        reducer_traces, reducer_records = self.reduce_assets((signed,))
        _logger.debug(
            "FactReducer produced %d trace consequences for asset %r",
            len(reducer_traces),
            signed.name,
        )

        all_traces = trace_entries + reducer_traces
        self._store.commit_ingress_batch(
            accepted_assets=[signed],
            trace_entries=all_traces,
            runtime_records=(asset_record,)
            + reducer_records
            + trace_records(all_traces),
        )
        if self._trace is not self._store:
            for entry in all_traces:
                self._trace.append(entry)

        return signed

    def commit_execution_batch(
        self,
        assets: list[Asset],
        engine_trace_entries: Sequence[object],
        *,
        source: str = "projection",
        allow_protected: bool = False,
    ) -> list[Asset]:
        signed: list[Asset] = []
        for asset in assets:
            if not allow_protected:
                prefix = _get_matched_prefix(asset.name)
                if prefix is not None:
                    raise ReservedNamespaceError(asset.name, prefix)
            s = sign_asset(asset)
            if not s.signed_by:
                s = sign_asset(asset, signed_by="engine")
            signed.append(s)

        ingress_traces: list[object] = []
        for s_asset in signed:
            entry = create_entry(
                contract_id="runtime_ingress",
                event_type="asset_accepted",
                parent_id=s_asset.id,
                relation_target=s_asset.name,
                accepted_fragments=[
                    json.dumps(
                        {
                            "asset_id": s_asset.id,
                            "name": s_asset.name,
                            "origin": s_asset.origin,
                            "trust_tier": s_asset.trust_tier,
                            "source": source,
                        }
                    )
                ],
            )
            ingress_traces.append(entry)

        reducer_traces, reducer_records = self.reduce_assets(signed)
        all_traces = list(engine_trace_entries) + ingress_traces + reducer_traces
        self._store.commit_ingress_batch(
            accepted_assets=signed,
            trace_entries=all_traces,
            runtime_records=tuple(asset_committed_record(asset) for asset in signed)
            + reducer_records
            + trace_records(all_traces),
        )
        if self._trace is not self._store:
            for entry in ingress_traces + reducer_traces:
                self._trace.append(entry)

        return signed

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
        validate_contract_commitment(contract, require_canonical_v3=False)

        entry = create_entry(
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

        self._store.commit_ingress_batch(
            accepted_assets=[],
            trace_entries=[entry],
            contract=contract,
            runtime_records=(
                create_runtime_record(
                    "contract.declared", {"contract": contract_to_dict(contract)}
                ),
                *trace_records([entry]),
            ),
        )
        if self._trace is not self._store:
            self._trace.append(entry)

        return contract

    # -- Replacement claim acceptance ---------------------------------------

    def accept_replacement_claim(
        self,
        claim: ReplacementClaim,
        *,
        source: str = "ingress",
    ) -> ReplacementClaim:
        """Accept an additive asset replacement/version claim.

        Replacement claims are control facts.  They do not mutate either
        referenced asset; they record an auditable relationship that readers
        may use for version resolution, slicing, summaries, or redactions.
        """
        entry = create_entry(
            contract_id="runtime_ingress",
            event_type="replacement_claim_created",
            parent_id=claim.id,
            relation_type=claim.claim_type,
            relation_target=claim.replacement_asset_id,
            accepted_fragments=[
                json.dumps(
                    {
                        "claim_id": claim.id,
                        "source_asset_id": claim.source_asset_id,
                        "replacement_asset_id": claim.replacement_asset_id,
                        "definition_hash": claim.definition_hash,
                        "claim_type": claim.claim_type,
                        "source": source,
                    },
                    sort_keys=True,
                )
            ],
        )
        self._store.commit_replacement_claim(claim, entry)
        self._trace.append(entry)
        return claim
