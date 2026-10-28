"""In-memory and persistent key-value stores for Assets and Contracts."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

from aigineering.core.authority import (
    ReservedNamespaceError,
    _is_protected_name,
    matched_reserved_prefix,
)
from aigineering.core.actor_facts import validate_actor_runtime_record
from aigineering.core.asset_graph_facts import validate_asset_graph_record
from aigineering.core.activation import activation_names
from aigineering.core.asset_versions import (
    replacement_claim_from_record,
    replacement_claim_payload,
)
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.ids import compute_content_hash, validate_contract_identity
from aigineering.core.lifecycle_facts import validate_terminal_record
from aigineering.core.worker_routing import (
    registration_is_replay,
    registration_from_record,
    worker_registration_record,
)
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    create_runtime_record,
    runtime_record_effective_payload,
    validate_runtime_record,
)
from aigineering.protocol.wire import asset_to_dict, contract_to_dict
from aigineering.core.store_capabilities import (
    AssetReader as AssetReader,
    CandidateCommitStore,
    ContractReader as ContractReader,
    RuntimeRecordReader as RuntimeRecordReader,
    TraceReader,
    WorkerClaimStore,
)

_logger = logging.getLogger(__name__)


@runtime_checkable
class StoreProtocol(CandidateCommitStore, Protocol):
    """Protocol that any store (in-memory or persistent) must satisfy."""

    def add_asset(self, asset: Asset) -> None: ...
    def add_contract(self, contract: Contract) -> None: ...
    def add_replacement_claim(self, claim) -> None: ...
    def register_activation_refs(
        self, contract_id: str, asset_names: set[str]
    ) -> None: ...
    def unregister_activation_refs(self, contract_id: str) -> None: ...
    def register_declared_outputs(
        self, contract_id: str, output_names: set[str]
    ) -> None: ...
    def unregister_declared_outputs(self, contract_id: str) -> None: ...
    def rebuild_projection_indexes(self) -> None: ...
    def append_runtime_record(self, record: RuntimeRecord) -> int: ...
    def get_claim_history_contract_ids(self) -> frozenset[str]: ...
    def commit_replacement_claim(self, claim, trace_entry) -> None: ...


@runtime_checkable
class OperationalStoreProtocol(StoreProtocol, WorkerClaimStore, TraceReader, Protocol):
    """Required transactional surface for claim/package/submit execution."""

    def new_entry(self, contract_id: str, event_type: str, **kwargs): ...
    def get_idempotency(
        self, contract_id: str, idempotency_key: str
    ) -> dict | None: ...
    def has_any_idempotency(self, contract_id: str) -> bool: ...


def require_runtime_store(store: object) -> StoreProtocol:
    """Reject adapters that cannot preserve canonical ingress semantics."""
    if not isinstance(store, StoreProtocol):
        raise TypeError(
            f"{type(store).__name__} does not implement the required runtime StorePort"
        )
    return store


def require_operational_store(store: StoreProtocol) -> OperationalStoreProtocol:
    """Fail startup instead of silently downgrading transactional semantics."""
    if not isinstance(store, OperationalStoreProtocol):
        raise TypeError(
            f"{type(store).__name__} does not implement the required "
            "transactional worker StorePort"
        )
    return store


# ---------------------------------------------------------------------------
# Activation name extraction
# ---------------------------------------------------------------------------


def _projection_index_digest(
    activation_rows: list[tuple[str, str]], output_rows: list[tuple[str, str]]
) -> str:
    payload = json.dumps(
        {
            "activation": sorted(activation_rows),
            "declared_outputs": sorted(output_rows),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return compute_content_hash(payload)


class _ProjectionIndexMixin:
    """Shared derived-index behavior for local record adapters."""

    contracts: dict[str, Contract]
    _activation_index: dict[str, set[str]]
    _reverse_activation_index: dict[str, set[str]]
    _declared_outputs_index: dict[str, set[str]]

    def register_activation_refs(self, contract_id: str, asset_names: set[str]) -> None:
        self.unregister_activation_refs(contract_id)
        if not asset_names:
            return
        self._activation_index[contract_id] = asset_names.copy()
        for name in asset_names:
            self._reverse_activation_index.setdefault(name, set()).add(contract_id)

    def unregister_activation_refs(self, contract_id: str) -> None:
        old = self._activation_index.pop(contract_id, None)
        if old:
            for name in old:
                targets = self._reverse_activation_index.get(name)
                if targets:
                    targets.discard(contract_id)
                    if not targets:
                        del self._reverse_activation_index[name]

    def register_declared_outputs(
        self, contract_id: str, output_names: set[str]
    ) -> None:
        self.unregister_declared_outputs(contract_id)
        for name in output_names:
            self._declared_outputs_index.setdefault(name, set()).add(contract_id)

    def unregister_declared_outputs(self, contract_id: str) -> None:
        empty_names: list[str] = []
        for name, contract_ids in self._declared_outputs_index.items():
            contract_ids.discard(contract_id)
            if not contract_ids:
                empty_names.append(name)
        for name in empty_names:
            del self._declared_outputs_index[name]

    def get_contracts_waiting_for(self, asset_name: str) -> list[str]:
        return list(self._reverse_activation_index.get(asset_name, set()))

    def get_contracts_declaring_output(self, asset_name: str) -> list[str]:
        return list(self._declared_outputs_index.get(asset_name, set()))

    def rebuild_projection_indexes(self) -> None:
        """Rebuild all derived indexes solely from immutable contracts."""
        self._activation_index.clear()
        self._reverse_activation_index.clear()
        self._declared_outputs_index.clear()
        for contract in self.contracts.values():
            self.register_activation_refs(
                contract.id, activation_names(contract.activation)
            )
            self.register_declared_outputs(contract.id, set(contract.outputs))

    def projection_index_digest(self) -> str:
        activation_rows = [
            (contract_id, asset_name)
            for contract_id, asset_names in self._activation_index.items()
            for asset_name in asset_names
        ]
        output_rows = [
            (contract_id, output_name)
            for output_name, contract_ids in self._declared_outputs_index.items()
            for contract_id in contract_ids
        ]
        return _projection_index_digest(activation_rows, output_rows)


class MemoryStore(_ProjectionIndexMixin):
    def __init__(self) -> None:
        self.assets: dict[str, Asset] = {}
        self.contracts: dict[str, Contract] = {}
        self._claims: list = []
        self._activation_index: dict[str, set[str]] = {}
        self._reverse_activation_index: dict[str, set[str]] = {}
        self._declared_outputs_index: dict[str, set[str]] = {}
        self._worker_registrations: dict[str, object] = {}
        self._runtime_records: dict[str, tuple[int, RuntimeRecord]] = {}
        self._runtime_revision = 0

    def register_worker(self, registration) -> None:
        self.append_runtime_record(worker_registration_record(registration))

    def get_worker_registration(self, worker_id: str):
        return self._worker_registrations.get(worker_id)

    def get_worker_registrations(self) -> list:
        return list(self._worker_registrations.values())

    def get_claim_history_contract_ids(self) -> frozenset[str]:
        """Return claim history represented by this non-operational adapter."""
        return frozenset(
            str(record.payload.get("contract_id", ""))
            for _, record in self.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("contract_id")
        )

    def rebuild_worker_registration_projection(self) -> None:
        self._worker_registrations.clear()
        for _, record in self.scan_runtime_records(record_type="worker.registered"):
            registration = registration_from_record(record)
            self._worker_registrations[registration.worker_id] = registration

    def append_runtime_record(self, record: RuntimeRecord) -> int:
        if record.record_type in {
            "allowance.granted",
            "allowance.reserved",
            "allowance.returned",
            "allowance.extinguished",
        }:
            raise ValueError("allowance facts require Candidate ingress commitment")
        if record.record_type in {
            "claim.constraint.placed",
            "claim.constraint.released",
        }:
            raise ValueError(
                "claim constraint facts require Candidate ingress commitment"
            )
        if record.record_type == "claim.assignment.changed":
            raise ValueError("assignment facts require Candidate ingress commitment")
        return self._append_runtime_record(record)

    def _append_runtime_record(self, record: RuntimeRecord) -> int:
        """Append from a Store-owned transaction after its ingress gate."""
        existing = self._runtime_records.get(record.id)
        if existing is not None:
            revision, existing_record = existing
            if runtime_record_effective_payload(
                existing_record
            ) != runtime_record_effective_payload(record):
                raise ImmutableRecordConflict("runtime record", record.id)
            registration = (
                registration_from_record(existing_record)
                if existing_record.record_type == "worker.registered"
                else None
            )
            if registration is not None:
                self._worker_registrations[registration.worker_id] = registration
            replacement_claim = replacement_claim_from_record(existing_record)
            if replacement_claim is not None:
                self.add_replacement_claim(replacement_claim)
            return revision
        validate_actor_runtime_record(record, self)
        validate_asset_graph_record(record, self)
        if record.record_type == "lifecycle.terminal":
            validate_terminal_record(
                record,
                self.scan_runtime_records(record_type="lifecycle.terminal"),
            )
            contract_id = str(record.payload["contract_id"])
            if self.get_contract(contract_id) is None:
                raise ValueError(
                    f"lifecycle.terminal references unknown Contract {contract_id!r}"
                )
        registration = None
        if record.record_type == "worker.registered":
            registration = registration_from_record(record)
            registration_is_replay(
                self.scan_runtime_records(record_type="worker.registered"),
                registration,
            )
        replacement_claim = replacement_claim_from_record(record)
        validate_runtime_record(record)
        self._runtime_revision += 1
        self._runtime_records[record.id] = (self._runtime_revision, record)
        if registration is not None:
            self._worker_registrations[registration.worker_id] = registration
        if replacement_claim is not None:
            self.add_replacement_claim(replacement_claim)
        return self._runtime_revision

    def get_runtime_record(self, record_id: str) -> RuntimeRecord | None:
        existing = self._runtime_records.get(record_id)
        return existing[1] if existing is not None else None

    def scan_runtime_records(
        self,
        *,
        after_revision: int = 0,
        through_revision: int | None = None,
        record_type: str | None = None,
    ) -> list[tuple[int, RuntimeRecord]]:
        return [
            (revision, record)
            for revision, record in self._runtime_records.values()
            if revision > after_revision
            and (through_revision is None or revision <= through_revision)
            and (record_type is None or record.record_type == record_type)
        ]

    def get_runtime_revision(self) -> int:
        return self._runtime_revision

    def read_contract_declaration_projection_inputs(self, request):
        """Read only exact declaration inputs and relevant runtime facts."""
        from aigineering.core.projection_context import (
            ContractDeclarationProjectionInputs,
        )

        contract_ids = tuple(
            dict.fromkeys(
                (*request.incoming_contract_ids, *request.parent_contract_ids)
            )
        )
        contracts = tuple(
            contract
            for contract_id in contract_ids
            if (contract := self.get_contract(contract_id)) is not None
        )
        assets = tuple(
            asset
            for asset_id in request.asset_ids
            if (asset := self.get_asset(asset_id)) is not None
        )

        source_ids = set(request.allowance_source_contract_ids)
        selected: dict[str, tuple[int, RuntimeRecord]] = {}
        heads: list[RuntimeRecord] = []

        def source_matches(payload, field: str) -> bool:
            value = payload.get(field)
            return isinstance(value, str) and value in source_ids

        for revision, record in self._runtime_records.values():
            payload = record.payload
            matched = record.record_type == "allowance.granted" and (
                source_matches(payload, "contract_id")
            )
            matched = (
                matched
                or record.record_type == "allowance.reserved"
                and source_matches(payload, "source_contract_id")
            )
            matched = (
                matched
                or record.record_type == "allowance.returned"
                and (
                    source_matches(payload, "source_contract_id")
                    or source_matches(payload, "from_contract_id")
                )
            )
            matched = (
                matched
                or record.record_type == "allowance.extinguished"
                and source_matches(payload, "contract_id")
            )
            replay_match = (
                request.include_candidate_replay
                and record.record_type in {"candidate.received", "candidate.committed"}
                and payload.get("candidate_id") == request.candidate_id
            )
            if matched or replay_match:
                selected[record.id] = (revision, record)
                if record.record_type == "candidate.committed":
                    heads.append(record)

        member_ids = {
            member_id
            for head in heads
            if isinstance(
                (member_ids_value := head.payload.get("committed_record_ids")),
                (list, tuple),
            )
            for member_id in member_ids_value
            if isinstance(member_id, str)
        }
        for member_id in member_ids:
            found = self._runtime_records.get(member_id)
            if found is not None:
                selected[member_id] = found
        runtime_records = tuple(
            record for _, record in sorted(selected.values(), key=lambda item: item[0])
        )
        return ContractDeclarationProjectionInputs(
            contracts=contracts,
            assets=assets,
            runtime_records=runtime_records,
        )

    def _graph_payloads(self, position: int) -> list[dict]:
        from aigineering.core.asset_graph_facts import graph_rows_from_record

        values = {
            str(payload["id"]): payload
            for _, record in self.scan_runtime_records()
            if (payload := graph_rows_from_record(record)[position]) is not None
        }
        return [values[key] for key in sorted(values)]

    def get_content_objects(self) -> list[dict]:
        return self._graph_payloads(0)

    def get_asset_definitions(self) -> list[dict]:
        return self._graph_payloads(1)

    def get_definition_content_assertions(
        self, *, definition_id: str = "", content_id: str = ""
    ) -> list[dict]:
        return [
            assertion
            for assertion in self._graph_payloads(2)
            if (not definition_id or assertion["definition_id"] == definition_id)
            and (not content_id or assertion["content_id"] == content_id)
        ]

    def commit_ingress_batch(
        self,
        accepted_assets: list[Asset],
        trace_entries: list,
        *,
        contract: Contract | None = None,
        contracts: tuple[Contract, ...] = (),
        reducer_callback=None,
        runtime_records: tuple[RuntimeRecord, ...] = (),
        claim_binding=None,
        candidate_actor_id: str = "",
        candidate_key_id: str = "",
        candidate_id: str = "",
        candidate_proposal=None,
        effect_fact_owners=(),
        assignment_intents=(),
        successor_assignment_witnesses=(),
        validate_snapshot: Callable[[], None] | None = None,
    ) -> tuple[tuple, tuple]:
        """Apply one ingress batch with rollback-equivalent memory semantics."""
        if claim_binding is not None:
            raise TypeError(
                "MemoryStore does not implement transactional claim fencing"
            )
        if candidate_id and any(
            record.record_type == "candidate.committed"
            and record.payload.get("candidate_id") == candidate_id
            for _, record in self.scan_runtime_records()
        ):
            return (), ()
        if validate_snapshot is not None:
            validate_snapshot()
        has_assignment_facts = any(
            record.record_type == "claim.assignment.changed"
            for record in runtime_records
        )
        if has_assignment_facts or assignment_intents or successor_assignment_witnesses:
            if validate_snapshot is None or candidate_proposal is None:
                raise ValueError(
                    "assignment facts require their signed Candidate projection"
                )
            from aigineering.core.claim_assignments import (
                assignment_context_from_projection,
                validate_assignment_batch,
            )
            from aigineering.core.projection_context import (
                load_effect_projection_context,
            )

            context = assignment_context_from_projection(
                load_effect_projection_context(self)
            )
            receipt = next(
                (
                    record
                    for record in runtime_records
                    if record.record_type == "candidate.received"
                    and record.payload.get("candidate_id") == candidate_id
                ),
                None,
            )
            if receipt is None:
                raise ValueError("assignment Candidate receipt is missing")
            validate_assignment_batch(
                context,
                runtime_records,
                contracts,
                candidate=candidate_proposal,
                receipt_id=receipt.id,
                effect_fact_owners=tuple(effect_fact_owners),
                assignment_intents=tuple(assignment_intents),
                successor_assignment_witnesses=tuple(successor_assignment_witnesses),
            )
        del trace_entries
        declarations = ((contract,) if contract is not None else ()) + tuple(contracts)
        from aigineering.core.causal_allowance import materialize_terminal_allowance

        derived_records = materialize_terminal_allowance(
            self, declarations, runtime_records
        )
        runtime_records = runtime_records + derived_records
        if any(
            record.record_type
            in {"claim.constraint.placed", "claim.constraint.released"}
            for record in runtime_records
        ):
            from aigineering.core.claim_constraints import (
                validate_claim_constraint_batch,
            )
            from aigineering.core.output_satisfaction import all_outputs_satisfied

            existing_records = tuple(
                record for _, record in self.scan_runtime_records()
            )
            all_contracts = tuple(self.get_all_contracts()) + declarations
            known_contract_ids = frozenset(item.id for item in all_contracts)
            claimed_ids = frozenset(
                str(record.payload.get("contract_id", ""))
                for record in existing_records
                if record.record_type == "claim.granted"
            )
            terminal_ids = frozenset(
                str(record.payload.get("contract_id", ""))
                for record in existing_records
                if record.record_type == "lifecycle.terminal"
            )
            output_satisfied_ids = frozenset(
                item.id
                for item in all_contracts
                if all_outputs_satisfied(item, self, require_outputs=True)
            )
            validate_claim_constraint_batch(
                existing_records,
                runtime_records,
                candidate_id=candidate_id,
                actor_id=candidate_actor_id,
                key_id=candidate_key_id,
                known_contract_ids=known_contract_ids,
                claimed_contract_ids=claimed_ids,
                terminal_contract_ids=terminal_ids,
                output_satisfied_contract_ids=output_satisfied_ids,
            )
        if any(
            record.record_type
            in {
                "allowance.granted",
                "allowance.reserved",
                "allowance.returned",
                "allowance.extinguished",
            }
            for record in runtime_records
        ):
            from aigineering.core.causal_allowance import validate_allowance_commit

            validate_allowance_commit(
                tuple(self.get_all_contracts()) + declarations,
                tuple(record for _, record in self.scan_runtime_records()),
                runtime_records,
                candidate_id=candidate_id,
            )
        del candidate_actor_id, candidate_key_id, candidate_id
        if any(record.record_type == "output.qualified" for record in runtime_records):
            from aigineering.core.acceptance import (
                validate_output_qualification_commit,
            )

            validate_output_qualification_commit(
                tuple(record for _, record in self.scan_runtime_records()),
                runtime_records,
            )
        for record in runtime_records:
            validate_runtime_record(record)
        snapshot = (
            self.assets.copy(),
            self.contracts.copy(),
            {key: value.copy() for key, value in self._activation_index.items()},
            {
                key: value.copy()
                for key, value in self._reverse_activation_index.items()
            },
            {key: value.copy() for key, value in self._declared_outputs_index.items()},
            self._runtime_records.copy(),
            self._runtime_revision,
            self._worker_registrations.copy(),
        )
        committed = False
        try:
            for declaration in declarations:
                self.add_contract(declaration)
            for asset in accepted_assets:
                if not asset.signed_by or not verify_asset_seal(asset):
                    raise ValueError(f"Asset '{asset.id}' has an invalid seal")
                self._persist_asset(asset)
            if reducer_callback is not None:
                reducer_callback()
            for record in runtime_records:
                self._append_runtime_record(record)
            committed = True
            return (), derived_records
        finally:
            if not committed:
                (
                    self.assets,
                    self.contracts,
                    self._activation_index,
                    self._reverse_activation_index,
                    self._declared_outputs_index,
                    self._runtime_records,
                    self._runtime_revision,
                    self._worker_registrations,
                ) = snapshot

    def add_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        if _is_protected_name(asset.name):
            prefix = matched_reserved_prefix(asset.name) or "?"
            raise ReservedNamespaceError(asset.name, prefix)
        existing = self.assets.get(asset.id)
        if existing is not None:
            if existing == asset:
                return
            raise ImmutableRecordConflict("asset", asset.id)
        self.assets[asset.id] = asset

    def _add_system_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        self._persist_asset(asset)

    def _persist_asset(self, asset: Asset) -> None:
        existing = self.assets.get(asset.id)
        if existing is not None:
            if existing == asset:
                return
            raise ImmutableRecordConflict("asset", asset.id)
        self.assets[asset.id] = asset

    def get_asset(self, asset_id: str) -> Optional[Asset]:
        return self.assets.get(asset_id)

    def get_assets_by_name(self, name: str) -> list[Asset]:
        return [a for a in self.assets.values() if a.name == name]

    def has_asset_named(self, name: str) -> bool:
        return any(a.name == name for a in self.assets.values())

    def get_all_assets(self) -> list[Asset]:
        return list(self.assets.values())

    def add_contract(self, contract: Contract) -> None:
        validate_contract_identity(contract)
        existing = self.contracts.get(contract.id)
        if existing is not None:
            if existing == contract:
                return
            raise ImmutableRecordConflict("contract", contract.id)
        self.contracts[contract.id] = contract
        self.register_activation_refs(
            contract.id, activation_names(contract.activation)
        )
        self.register_declared_outputs(contract.id, set(contract.outputs))

    def get_contract(self, contract_id: str) -> Optional[Contract]:
        return self.contracts.get(contract_id)

    def get_all_contracts(self) -> list[Contract]:
        return list(self.contracts.values())

    def get_assets_by_contract(self, contract_id: str) -> list[Asset]:
        return [a for a in self.assets.values() if a.created_by == contract_id]

    def get_assets_by_definition(self, def_hash: str) -> list[Asset]:
        return [a for a in self.assets.values() if a.definition_hash == def_hash]

    def get_latest_asset(self, def_hash: str) -> Optional[Asset]:
        latest: Optional[Asset] = None
        for asset in self.assets.values():
            if asset.definition_hash == def_hash:
                latest = asset
        return latest

    def add_replacement_claim(self, claim) -> None:
        for existing in self._claims:
            if existing.id != claim.id:
                continue
            if existing == claim:
                return
            raise ImmutableRecordConflict("replacement claim", claim.id)
        self._claims.append(claim)

    def commit_replacement_claim(self, claim, trace_entry) -> None:
        self.add_replacement_claim(claim)
        self.append_runtime_record(
            create_runtime_record(
                "replacement.claimed",
                {"claim": replacement_claim_payload(claim)},
            )
        )
        from aigineering.protocol.wire import trace_entry_to_dict

        self.append_runtime_record(
            create_runtime_record(
                "trace.recorded", {"trace": trace_entry_to_dict(trace_entry)}
            )
        )

    def get_claims_by_definition(self, definition_hash: str) -> list:
        return [c for c in self._claims if c.definition_hash == definition_hash]

    def get_claims_for_asset(self, asset_id: str) -> list:
        return [c for c in self._claims if c.source_asset_id == asset_id]

    def get_claims_for_replacement_asset(self, asset_id: str) -> list:
        return [c for c in self._claims if c.replacement_asset_id == asset_id]


class JsonLStore(_ProjectionIndexMixin):
    """Persistent JSONL store for Assets and Contracts — one JSON object per line."""

    def __init__(self, assets_path: str, contracts_path: str) -> None:
        self._assets_path = assets_path
        self._contracts_path = contracts_path

        for p in (assets_path, contracts_path):
            parent = Path(p).parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)

        self.assets: dict[str, Asset] = {}
        self.contracts: dict[str, Contract] = {}
        self._name_index: dict[str, list[str]] = {}
        self._created_by_index: dict[str, list[str]] = {}
        self._activation_index: dict[str, set[str]] = {}
        self._reverse_activation_index: dict[str, set[str]] = {}
        self._declared_outputs_index: dict[str, set[str]] = {}

        self._load_assets()
        self._load_contracts()
        self.rebuild_projection_indexes()

    def _load_assets(self) -> None:
        if not os.path.exists(self._assets_path):
            return
        with open(self._assets_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                data = json.loads(stripped)
                asset = Asset(
                    id=data["id"],
                    name=data["name"],
                    content=data["content"],
                    content_type=data.get("content_type", "text"),
                    created_by=data.get("created_by", ""),
                    origin=data.get("origin", ""),
                    trust_tier=data.get("trust_tier", "untrusted"),
                    minted_by=data.get("minted_by", ""),
                    source_uri=data.get("source_uri", ""),
                    signed_by=data.get("signed_by", ""),
                    signer_kind=data.get("signer_kind", "deterministic"),
                    provenance_seal=data.get(
                        "provenance_seal", data.get("signature", "")
                    ),
                    definition_hash=data.get("definition_hash", ""),
                    content_hash=data.get("content_hash", ""),
                    promptable=data.get("promptable", True),
                    disclosure_view=data.get("disclosure_view", "original"),
                    keep_flag=data.get("keep_flag", False),
                    tombstoned=data.get("tombstoned", False),
                    tombstoned_at=data.get("tombstoned_at"),
                    lineage_id=data.get("lineage_id", ""),
                )
                existing = self.assets.get(asset.id)
                if existing is not None:
                    if existing == asset:
                        continue
                    raise ImmutableRecordConflict("asset", asset.id)
                self.assets[asset.id] = asset
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self._name_index.clear()
        self._created_by_index.clear()
        for asset in self.assets.values():
            self._name_index.setdefault(asset.name, []).append(asset.id)
            if asset.created_by:
                self._created_by_index.setdefault(asset.created_by, []).append(asset.id)

    def _load_contracts(self) -> None:
        if not os.path.exists(self._contracts_path):
            return
        with open(self._contracts_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                data = json.loads(stripped)
                contract = Contract(
                    id=data["id"],
                    parent_id=data.get("parent_id"),
                    name=data.get("name", ""),
                    description=data.get("description", ""),
                    inputs=data.get("inputs", []),
                    outputs=data.get("outputs", []),
                    activation=data.get("activation", ""),
                    budget=data.get("budget", 0),
                    tool_scope=data.get("tool_scope", []),
                    labels=data.get("labels", []),
                    context_asset_ids=data.get("context_asset_ids", []),
                    worker_capabilities=data.get("worker_capabilities", []),
                    worker_pools=data.get("worker_pools", []),
                    delegation_capabilities=data.get("delegation_capabilities", []),
                    delegation_pools=data.get("delegation_pools", []),
                    origin=data.get("origin", "human"),
                    minting_authority=data.get("minting_authority", []),
                    sensitive_input_policy=data.get("sensitive_input_policy"),
                    acceptance_policy=data.get("acceptance_policy"),
                )
                existing = self.contracts.get(contract.id)
                if existing is not None:
                    if existing == contract:
                        continue
                    raise ImmutableRecordConflict("contract", contract.id)
                self.contracts[contract.id] = contract

    @staticmethod
    def _write_jsonl_line(path: str, line: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                _logger.warning("fsync failed for %s", path)

    def add_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        if _is_protected_name(asset.name):
            prefix = matched_reserved_prefix(asset.name) or "?"
            raise ReservedNamespaceError(asset.name, prefix)
        self._persist_asset(asset)

    def _add_system_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        self._persist_asset(asset)

    def _persist_asset(self, asset: Asset) -> None:
        existing = self.assets.get(asset.id)
        if existing is not None:
            if existing == asset:
                return
            raise ImmutableRecordConflict("asset", asset.id)
        line = json.dumps(asset_to_dict(asset), ensure_ascii=False) + "\n"
        self._write_jsonl_line(self._assets_path, line)
        self.assets[asset.id] = asset
        self._name_index.setdefault(asset.name, []).append(asset.id)
        if asset.created_by:
            self._created_by_index.setdefault(asset.created_by, []).append(asset.id)

    def get_asset(self, asset_id: str) -> Optional[Asset]:
        return self.assets.get(asset_id)

    def get_assets_by_name(self, name: str) -> list[Asset]:
        ids = self._name_index.get(name, [])
        return [self.assets[aid] for aid in ids if aid in self.assets]

    def has_asset_named(self, name: str) -> bool:
        return name in self._name_index and len(self._name_index[name]) > 0

    def get_all_assets(self) -> list[Asset]:
        return list(self.assets.values())

    def get_assets_by_contract(self, contract_id: str) -> list[Asset]:
        ids = self._created_by_index.get(contract_id, [])
        return [self.assets[aid] for aid in ids if aid in self.assets]

    def get_assets_by_definition(self, def_hash: str) -> list[Asset]:
        return [a for a in self.assets.values() if a.definition_hash == def_hash]

    def get_latest_asset(self, def_hash: str) -> Optional[Asset]:
        latest: Optional[Asset] = None
        for asset in self.assets.values():
            if asset.definition_hash == def_hash:
                latest = asset
        return latest

    def add_contract(self, contract: Contract) -> None:
        validate_contract_identity(contract)
        existing = self.contracts.get(contract.id)
        if existing is not None:
            if existing == contract:
                return
            raise ImmutableRecordConflict("contract", contract.id)
        line = json.dumps(contract_to_dict(contract), ensure_ascii=False) + "\n"
        self._write_jsonl_line(self._contracts_path, line)
        self.contracts[contract.id] = contract
        self.register_activation_refs(
            contract.id, activation_names(contract.activation)
        )
        self.register_declared_outputs(contract.id, set(contract.outputs))

    def get_contract(self, contract_id: str) -> Optional[Contract]:
        return self.contracts.get(contract_id)

    def get_all_contracts(self) -> list[Contract]:
        return list(self.contracts.values())
