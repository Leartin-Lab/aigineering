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
from collections.abc import Sequence
from typing import TYPE_CHECKING

from aigineering.core.activation import validate_execution_activation
from aigineering.core.authority import RESERVED_PREFIXES, ReservedNamespaceError
from aigineering.core.provenance import sign_asset
from aigineering.core.trace import create_entry
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.wire import (
    asset_to_dict,
    contract_to_dict,
    trace_entry_to_dict,
)

if TYPE_CHECKING:
    from aigineering.core.fact_reducer import FactReducer
    from aigineering.protocol.types import (
        Asset,
        Candidate,
        Contract,
        ProjectionResult,
        ReplacementClaim,
    )
    from aigineering.core.store import StoreProtocol
    from aigineering.core.trace import TraceStoreProtocol

_logger = logging.getLogger(__name__)


def _is_protected_name(name: str) -> bool:
    """Return True when *name* starts with a protected prefix."""
    return _get_matched_prefix(name) is not None


def _get_matched_prefix(name: str) -> str | None:
    """Return the first matching protected prefix, or None."""
    for prefix in RESERVED_PREFIXES:
        if name.startswith(prefix):
            return prefix
        # Also match the bare prefix form: "_sys_" should match "_sys"
        if prefix.endswith("_") and name == prefix.rstrip("_"):
            return prefix
    return None


def _asset_committed_record(asset: Asset) -> RuntimeRecord:
    return create_runtime_record(
        "asset.committed",
        {"asset": asset_to_dict(asset), "contract_id": asset.created_by},
    )


def _trace_records(entries: Sequence[object]) -> tuple[RuntimeRecord, ...]:
    return tuple(
        create_runtime_record(
            "trace.recorded",
            {"trace": trace_entry_to_dict(entry)},
        )
        for entry in entries
    )


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
        asset_record = _asset_committed_record(signed)

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

        from collections.abc import Callable

        commit_fn: Callable | None = getattr(
            self._store, "commit_direct_execution", None
        )
        if commit_fn is not None:
            trace_entries: list[object] = [main_entry]
            if protected_entry is not None:
                trace_entries.append(protected_entry)

            # Collect reducer traces with mirror_to_trace=False — they'll
            # be mirrored to runtime memory only after the transaction
            # succeeds, matching the pattern in commit_execution_batch.
            reducer_traces: list[object] = []
            if self._reducer is not None:
                events = self._reducer.on_asset_created(signed)
                _logger.debug(
                    "FactReducer produced %d events for asset %r",
                    len(events),
                    signed.name,
                )
                reducer_traces.extend(
                    self._apply_reducer_events(events, signed, mirror_to_trace=False)
                )

            commit_fn(
                accepted_assets=[signed],
                trace_entries=list(trace_entries) + reducer_traces,
                runtime_records=(asset_record,)
                + _trace_records(list(trace_entries) + reducer_traces),
            )
            for entry in reducer_traces:
                self._trace.append(entry)
        else:
            if allow_protected:
                add_fn = getattr(
                    self._store, "_add_system_asset", self._store.add_asset
                )
                add_fn(signed)
            else:
                self._store.add_asset(signed)
            self._trace.append(main_entry)
            if protected_entry is not None:
                self._trace.append(protected_entry)
            reducer_traces: list[object] = []
            if self._reducer is not None:
                events = self._reducer.on_asset_created(signed)
                _logger.debug(
                    "FactReducer produced %d events for asset %r",
                    len(events),
                    signed.name,
                )
                reducer_traces = self._apply_reducer_events(events, signed)
            self._store.append_runtime_record(asset_record)
            for record in _trace_records(
                [main_entry]
                + ([protected_entry] if protected_entry is not None else [])
                + reducer_traces
            ):
                self._store.append_runtime_record(record)

        return signed

    def _apply_reducer_events(
        self, events: list[object], asset: Asset, *, mirror_to_trace: bool = True
    ) -> list[object]:
        """Apply FactReducer events: append trace entries for every
        detected consequence of the new asset.  Returns the created
        :class:`TraceEntry` objects for optional SQLite batch collection.

        When *mirror_to_trace* is False (used inside batch commit),
        the entries are only returned, not appended to the runtime trace
        store — the caller is responsible for mirroring after durable
        commit succeeds.
        """
        from aigineering.core.fact_reducer import FactReducerEvent

        created: list[object] = []
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
                entry = create_entry(
                    contract_id=event.contract_id,
                    event_type=trace_event_type,
                    parent_id=asset.id,
                    **trace_kwargs,
                )
                if mirror_to_trace:
                    self._trace.append(entry)
                created.append(entry)
        return created

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

        from collections.abc import Callable

        commit_fn: Callable | None = getattr(
            self._store, "commit_direct_execution", None
        )
        if commit_fn is not None:

            def _reducer_cb() -> list[object]:
                reducer_traces: list[object] = []
                if self._reducer is not None:
                    for s_asset in signed:
                        events = self._reducer.on_asset_created(s_asset)
                        created = self._apply_reducer_events(
                            events, s_asset, mirror_to_trace=False
                        )
                        reducer_traces.extend(created)
                return reducer_traces

            # Collect reducer traces once — used both for durable commit
            # and for runtime mirroring after the transaction succeeds.
            reducer_traces = _reducer_cb()
            all_traces = list(engine_trace_entries) + ingress_traces + reducer_traces
            commit_fn(
                accepted_assets=signed,
                trace_entries=all_traces,
                runtime_records=tuple(
                    _asset_committed_record(asset) for asset in signed
                )
                + _trace_records(all_traces),
            )
            for entry in ingress_traces:
                self._trace.append(entry)
            for entry in reducer_traces:
                self._trace.append(entry)
        else:
            add_fn = (
                getattr(self._store, "_add_system_asset", None)
                if allow_protected
                else None
            )
            for s_asset in signed:
                if add_fn is not None:
                    add_fn(s_asset)
                else:
                    self._store.add_asset(s_asset)
            reducer_traces: list[object] = []
            if self._reducer is not None:
                for s_asset in signed:
                    events = self._reducer.on_asset_created(s_asset)
                    reducer_traces.extend(self._apply_reducer_events(events, s_asset))
            for entry in ingress_traces:
                self._trace.append(entry)
            for s_asset in signed:
                self._store.append_runtime_record(_asset_committed_record(s_asset))
            for record in _trace_records(
                list(engine_trace_entries) + ingress_traces + reducer_traces
            ):
                self._store.append_runtime_record(record)

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
        validate_execution_activation(contract.activation)

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

        from collections.abc import Callable

        commit_fn: Callable | None = getattr(
            self._store, "commit_direct_execution", None
        )
        if commit_fn is not None:
            commit_fn(
                accepted_assets=[],
                trace_entries=[entry],
                contract=contract,
                runtime_records=(
                    create_runtime_record(
                        "contract.declared", {"contract": contract_to_dict(contract)}
                    ),
                    *_trace_records([entry]),
                ),
            )
        else:
            self._store.add_contract(contract)
            self._trace.append(entry)
            self._store.append_runtime_record(
                create_runtime_record(
                    "contract.declared", {"contract": contract_to_dict(contract)}
                )
            )
            self._store.append_runtime_record(_trace_records([entry])[0])

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
        commit_fn = getattr(self._store, "commit_replacement_claim", None)
        if commit_fn is not None:
            commit_fn(claim, entry)
        else:
            self._store.add_replacement_claim(claim)
            self._trace.append(entry)
        return claim

    # -- Candidate submission -----------------------------------------------

    def accept_candidate_submission(
        self,
        contract: Contract,
        candidate: Candidate,
        claim_id: str | None = None,
    ) -> ProjectionResult:
        """Reject the legacy claimless candidate-ingress API.

        Candidate commitment must use :func:`core.submit.submit_candidate`,
        irrespective of the backing store.  Keeping a permissive test-store
        branch here would make boundary guarantees depend on deployment
        configuration.
        """
        del contract, candidate, claim_id
        raise RuntimeError(
            "candidate submission is claim-bound; use "
            "aig worker submit / core.submit.submit_candidate"
        )
