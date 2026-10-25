"""SQLite transactional store for Assets, Contracts, and related entities.

Implements StoreProtocol with WAL-mode SQLite for concurrent reads,
schema versioning with migration hooks, and index coverage for all
lookup fields.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Optional

from aigineering.core.authority import (
    ReservedNamespaceError,
    _is_protected_name,
    matched_reserved_prefix,
)
from aigineering.core.activation import activation_names
from aigineering.core.claim_holds import (
    ClaimHoldConflict,
    open_claim_holds,
    validate_claim_hold_batch,
)
from aigineering.core.causal_allowance import (
    RawAllowanceTotals,
    _raw_integer_text,
    fold_raw_allowance_records,
)
from aigineering.core.crash import check_crash_point
from aigineering.core.ids import (
    compute_content_hash,
    now_iso,
    validate_contract_identity,
)
from aigineering.core.actor_facts import validate_actor_runtime_record
from aigineering.core.asset_graph_facts import (
    graph_rows_from_record,
    validate_asset_graph_record,
)
from aigineering.core.lifecycle_facts import validate_terminal_record
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.record_conflict import (
    ClaimBindingConflict,
    ImmutableRecordConflict,
)
from aigineering.core.asset_versions import (
    replacement_claim_from_record,
    replacement_claim_payload,
)
from aigineering.core.store import _projection_index_digest
from aigineering.core.sqlite_migrations import (
    CURRENT_SCHEMA_VERSION as CURRENT_SCHEMA_VERSION,
    current_schema_version,
    initialize_sqlite_schema,
)
from aigineering.core.sqlite_materialization import (
    asset_from_row,
    contract_from_row,
    replacement_claim_from_row,
    runtime_record_from_row,
)
from aigineering.core.trace import entry_references_asset, trace_effective_payload
from aigineering.core.worker_routing import (
    registration_is_replay,
    WorkerRegistration,
    registration_from_record,
    worker_registration_record,
)
from aigineering.protocol.types import Asset, Contract, ReplacementClaim, TraceEntry
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    create_runtime_record,
    runtime_record_effective_payload,
    validate_runtime_record,
)
from aigineering.protocol.wire import (
    asset_to_dict,
    contract_to_dict,
    trace_entry_from_dict,
    trace_entry_to_dict,
)

_logger = logging.getLogger(__name__)
_ASSET_ID_QUERY_CHUNK_SIZE = 900
_PROJECTION_ID_QUERY_RELATION_COUNT = 5


class WorkerBindingConflict(ValueError):
    """The registered worker actor/key changed before Candidate commitment."""


@dataclass(frozen=True)
class RawAllowanceCheckpointRead:
    status: str
    totals: RawAllowanceTotals | None
    through_revision: int
    tail_record_count: int
    decoded_record_count: int
    fold_record_count: int


def _is_sqlite_contention(exc: sqlite3.OperationalError) -> bool:
    """Return whether an OperationalError is expected writer contention."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    message = str(exc).lower()
    return "locked" in message or "busy" in message


# ---------------------------------------------------------------------------
# SQLiteStore
# ---------------------------------------------------------------------------


class SQLiteStore:
    """SQLite-backed store implementing StoreProtocol.

    Uses WAL mode for concurrent reads and explicit transactions for all
    writes.  Supports both in-memory (``:memory:``) and file-based
    databases.
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        allowance_checkpoint_mode: str = "checkpoint",
    ) -> None:
        if allowance_checkpoint_mode not in {"checkpoint", "reference"}:
            raise ValueError("unknown allowance checkpoint mode")
        self._allowance_checkpoint_mode = allowance_checkpoint_mode
        self._db_path = db_path

        if db_path != ":memory:":
            parent = Path(db_path).parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")

        # WAL only applies to file-backed databases; :memory: uses "memory" journal
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")

        try:
            initialize_sqlite_schema(self._conn, self)
        except Exception:
            self._conn.close()
            raise

    def snapshot(self) -> SQLiteStore:
        """Return an independent in-memory copy of this store.

        SQLite's backup API takes a consistent database snapshot, including
        committed WAL contents, without rebuilding or otherwise mutating the
        source projections. The returned store has its own connection and
        should be closed by its caller.
        """
        snapshot = SQLiteStore(
            ":memory:", allowance_checkpoint_mode=self._allowance_checkpoint_mode
        )
        try:
            self._conn.backup(snapshot._conn)
        except Exception:
            snapshot.close()
            raise
        return snapshot

    @classmethod
    def snapshot_from_path(cls, db_path: str | Path) -> SQLiteStore:
        """Open a read-only source database and copy it to an in-memory Store.

        The source is connected with SQLite ``mode=ro`` so construction cannot
        apply schema upgrades or repair materialized tables. A schema mismatch
        fails closed; callers receive only the isolated copy.
        """
        source_path = Path(db_path).resolve()
        if not source_path.is_file():
            raise FileNotFoundError("SQLite source database does not exist")
        source_uri = f"{source_path.as_uri()}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, check_same_thread=False)
        snapshot: SQLiteStore | None = None
        try:
            snapshot = cls(":memory:")
            source.backup(snapshot._conn)
            try:
                version = current_schema_version(snapshot._conn)
                tables = {
                    str(row[0])
                    for row in snapshot._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            except sqlite3.Error as exc:
                raise ValueError(
                    "SQLite source database schema is incompatible for read-only snapshot"
                ) from exc
            required_tables = {"assets", "contracts", "trace_events", "runtime_records"}
            if version != CURRENT_SCHEMA_VERSION or not required_tables <= tables:
                raise ValueError(
                    "SQLite source database schema is incompatible for read-only snapshot"
                )
            return snapshot
        except Exception:
            if snapshot is not None:
                snapshot.close()
            raise
        finally:
            source.close()

    # ── Immutable runtime-record envelope ────────────────────────────────

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
        with self._conn:
            revision = self._insert_runtime_record(record)
            if record.record_type == "lifecycle.terminal":
                check_crash_point("after_terminal_before_claim_release")
            self._release_claims_for_terminal_records((record,))
            return revision

    def _release_claims_for_terminal_records(
        self,
        records: tuple[RuntimeRecord, ...],
        *,
        submitting_claim_id: str = "",
    ) -> None:
        """Close claims invalidated by terminal facts in the same transaction."""
        terminal_records = {
            str(record.payload["contract_id"]): record
            for record in records
            if record.record_type == "lifecycle.terminal"
        }
        for contract_id, terminal in terminal_records.items():
            row = self._conn.execute(
                "SELECT claim_id, worker_id, epoch, package_id "
                "FROM worker_claims WHERE contract_id = ? AND status = 'active'",
                (contract_id,),
            ).fetchone()
            if row is None or row["claim_id"] == submitting_claim_id:
                continue
            recorded_at = now_iso()
            cursor = self._conn.execute(
                "UPDATE worker_claims SET status = 'released', updated_at = ? "
                "WHERE claim_id = ? AND epoch = ? AND status = 'active'",
                (recorded_at, row["claim_id"], int(row["epoch"])),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError(
                    "active claim changed during terminal commitment"
                )
            self._insert_runtime_record(
                create_runtime_record(
                    "claim.released",
                    {
                        "claim_id": row["claim_id"],
                        "contract_id": contract_id,
                        "epoch": int(row["epoch"]),
                        "package_id": row["package_id"],
                        "worker_id": row["worker_id"],
                    },
                    causal_parents=(
                        self._claim_granted_record_id(row["claim_id"]),
                        terminal.id,
                    ),
                    recorded_at=recorded_at,
                )
            )

    def _insert_runtime_record(
        self,
        record: RuntimeRecord,
        *,
        allow_claim_hold_fact: bool = False,
        allow_claim_constraint_fact: bool = False,
        allow_claim_assignment_fact: bool = False,
    ) -> int:
        """Insert within the caller's transaction without committing it."""
        if record.record_type.startswith("claim.hold."):
            if record.record_type not in {
                "claim.hold.placed",
                "claim.hold.released",
            }:
                raise ClaimHoldConflict("unsupported generic claim-hold fact")
            if not allow_claim_hold_fact:
                raise ClaimHoldConflict(
                    "claim-hold facts must enter through authenticated Candidate commitment"
                )
        if (
            record.record_type
            in {
                "claim.constraint.placed",
                "claim.constraint.released",
            }
            and not allow_claim_constraint_fact
        ):
            from aigineering.core.claim_constraints import ClaimConstraintConflict

            raise ClaimConstraintConflict(
                "claim constraint facts must enter through authenticated Candidate commitment"
            )
        if (
            record.record_type == "claim.assignment.changed"
            and not allow_claim_assignment_fact
        ):
            from aigineering.core.claim_assignments import AssignmentConflict

            raise AssignmentConflict(
                "assignment facts must enter through authenticated Candidate commitment"
            )
        row = self._conn.execute(
            "SELECT * FROM runtime_records WHERE record_id = ?", (record.id,)
        ).fetchone()
        if row is not None:
            existing = self._row_to_runtime_record(row)
            if runtime_record_effective_payload(
                existing
            ) != runtime_record_effective_payload(record):
                raise ImmutableRecordConflict("runtime record", record.id)
            registration = (
                registration_from_record(existing)
                if existing.record_type == "worker.registered"
                else None
            )
            if registration is not None:
                self._upsert_worker_registration(registration)
            replacement_claim = replacement_claim_from_record(existing)
            if replacement_claim is not None:
                self._insert_replacement_claim(replacement_claim)
            self._materialize_asset_graph_record(existing)
            return int(row["revision"])
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
        try:
            cursor = self._conn.execute(
                """INSERT INTO runtime_records (
                    record_id, record_type, schema_version, payload_json,
                    causal_parents, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.record_type,
                    record.schema_version,
                    json.dumps(
                        runtime_record_effective_payload(record)["payload"],
                        sort_keys=True,
                    ),
                    json.dumps(list(record.causal_parents)),
                    record.recorded_at,
                ),
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                "SELECT * FROM runtime_records WHERE record_id = ?", (record.id,)
            ).fetchone()
            if row is not None and runtime_record_effective_payload(
                self._row_to_runtime_record(row)
            ) == runtime_record_effective_payload(record):
                return int(row["revision"])
            raise ImmutableRecordConflict("runtime record", record.id) from None
        if registration is not None:
            self._upsert_worker_registration(registration)
        if replacement_claim is not None:
            self._insert_replacement_claim(replacement_claim)
        self._materialize_asset_graph_record(record)
        revision = int(cursor.lastrowid)
        self._refresh_allowance_checkpoint_for_record(record, revision)
        if record.record_type == "candidate.committed":
            self._refresh_publication_index_for_record(record, revision)
        return revision

    def _publication_index_tables_present(self) -> bool:
        names = {
            str(row[0])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {
            "runtime_records",
            "publication_head_members",
            "publication_index_state",
        } <= names

    def _publication_index_state(self):
        return self._conn.execute(
            "SELECT format_version, through_revision, basis_record_id, has_opaque_heads "
            "FROM publication_index_state WHERE singleton=1"
        ).fetchone()

    def _write_publication_index_state(
        self, through: int, basis: str, has_opaque: bool
    ) -> None:
        self._conn.execute(
            "INSERT INTO publication_index_state VALUES (1, 1, ?, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET format_version=excluded.format_version, "
            "through_revision=excluded.through_revision, basis_record_id=excluded.basis_record_id, "
            "has_opaque_heads=excluded.has_opaque_heads",
            (through, basis, int(has_opaque)),
        )

    def _index_publication_head(self, record: RuntimeRecord) -> bool:
        from aigineering.core.allowance_publication_index import (
            safe_committed_head_relations,
        )

        relations = safe_committed_head_relations(record)
        if relations is None:
            return False
        members, _parents = relations
        self._conn.executemany(
            "INSERT OR IGNORE INTO publication_head_members VALUES (?, ?)",
            ((record.id, member_id) for member_id in members),
        )
        return True

    def _refresh_publication_index_for_record(
        self, record: RuntimeRecord, revision: int
    ) -> None:
        if (
            record.record_type != "candidate.committed"
            or self._allowance_checkpoint_mode == "reference"
            or not self._publication_index_tables_present()
        ):
            return
        state = self._publication_index_state()
        if state is None:
            # A missing cursor is the only implicit cold-build case. The
            # accepted head is already in this transaction's immutable prefix.
            self.rebuild_publication_index()
            return
        if (
            type(state["format_version"]) is not int
            or state["format_version"] != 1
            or type(state["through_revision"]) is not int
            or state["through_revision"] < 0
            or state["through_revision"] >= revision
            or type(state["has_opaque_heads"]) is not int
            or state["has_opaque_heads"] not in (0, 1)
        ):
            return
        through = int(state["through_revision"])
        if through:
            basis = self._conn.execute(
                "SELECT record_id FROM runtime_records WHERE revision=?", (through,)
            ).fetchone()
            if basis is None or basis["record_id"] != state["basis_record_id"]:
                return
        elif state["basis_record_id"] != "":
            return
        tail = self._conn.execute(
            "SELECT * FROM runtime_records WHERE record_type='candidate.committed' "
            "AND revision>? AND revision<=? ORDER BY revision",
            (through, revision),
        ).fetchall()
        has_opaque = bool(state["has_opaque_heads"])
        for row in tail:
            head = self._row_to_runtime_record(row)
            if not self._index_publication_head(head):
                has_opaque = True
        basis_row = self._conn.execute(
            "SELECT record_id FROM runtime_records WHERE revision=?", (revision,)
        ).fetchone()
        if basis_row is not None:
            self._write_publication_index_state(
                revision, str(basis_row["record_id"]), has_opaque
            )

    def _publication_query_chunk_size(
        self,
        parameters_per_item: int,
        *,
        fixed_parameters: int = 0,
        maximum: int = 900,
    ) -> int:
        parameter_limit = self._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        available = parameter_limit - fixed_parameters
        chunk_size = min(maximum, available // parameters_per_item)
        if chunk_size < 1:
            raise sqlite3.OperationalError(
                "SQLite variable limit does not permit publication relation lookup"
            )
        return chunk_size

    def rebuild_publication_index(self) -> None:
        """Rebuild the derived head-member relation without owning outer transactions."""
        if not self._publication_index_tables_present():
            return
        owns_transaction = not self._conn.in_transaction
        if owns_transaction:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            through = self.get_runtime_revision()
            records = self._conn.execute(
                "SELECT * FROM runtime_records WHERE record_type='candidate.committed' "
                "AND revision<=? ORDER BY revision",
                (through,),
            ).fetchall()
            self._conn.execute("DELETE FROM publication_head_members")
            has_opaque = False
            for row in records:
                if not self._index_publication_head(self._row_to_runtime_record(row)):
                    has_opaque = True
            basis = ""
            if through:
                row = self._conn.execute(
                    "SELECT record_id FROM runtime_records WHERE revision=?", (through,)
                ).fetchone()
                if row is None:
                    raise sqlite3.IntegrityError("publication index basis is missing")
                basis = str(row["record_id"])
            self._write_publication_index_state(through, basis, has_opaque)
        except Exception:
            if owns_transaction:
                self._conn.execute("ROLLBACK")
            raise
        else:
            if owns_transaction:
                self._conn.execute("COMMIT")

    def audit_publication_index(self) -> tuple[str, ...]:
        """Audit the derived relation against one immutable runtime prefix."""
        if not self._publication_index_tables_present():
            return ()
        owns_snapshot = not self._conn.in_transaction
        if owns_snapshot:
            self._conn.execute("BEGIN")
        try:
            current_revision = self.get_runtime_revision()
            state = self._publication_index_state()
            if state is None:
                has_rows = self._conn.execute(
                    "SELECT 1 FROM publication_head_members LIMIT 1"
                ).fetchone()
                return (
                    ("publication index state is missing",)
                    if has_rows is not None
                    else ()
                )
            cursor = state["through_revision"]
            if (
                type(state["format_version"]) is not int
                or state["format_version"] != 1
                or type(cursor) is not int
                or cursor < 0
                or cursor > current_revision
                or not isinstance(state["basis_record_id"], str)
                or type(state["has_opaque_heads"]) is not int
                or state["has_opaque_heads"] not in (0, 1)
            ):
                return ("publication index integrity mismatch",)
            records = self._conn.execute(
                "SELECT * FROM runtime_records WHERE record_type='candidate.committed' "
                "AND revision<=? ORDER BY revision",
                (cursor,),
            ).fetchall()
            expected: set[tuple[str, str]] = set()
            opaque = False
            for row in records:
                record = self._row_to_runtime_record(row)
                from aigineering.core.allowance_publication_index import (
                    safe_committed_head_relations,
                )

                relations = safe_committed_head_relations(record)
                if relations is None:
                    opaque = True
                    continue
                expected.update((record.id, member) for member in relations[0])
            actual = {
                (str(row[0]), str(row[1]))
                for row in self._conn.execute(
                    "SELECT head_record_id, member_id FROM publication_head_members"
                )
            }
            basis = ""
            if cursor:
                row = self._conn.execute(
                    "SELECT record_id FROM runtime_records WHERE revision=?", (cursor,)
                ).fetchone()
                basis = "" if row is None else str(row["record_id"])
            if (
                type(state["format_version"]) is not int
                or state["format_version"] != 1
                or type(cursor) is not int
                or cursor < 0
                or cursor > current_revision
                or state["basis_record_id"] != basis
                or type(state["has_opaque_heads"]) is not int
                or state["has_opaque_heads"] != int(opaque)
                or actual != expected
            ):
                return ("publication index integrity mismatch",)
            return ()
        finally:
            if owns_snapshot:
                self._conn.execute("ROLLBACK")

    def read_allowance_publication_selection(self, request):
        """Execute one typed, snapshot-local publication relation lookup.

        ``None`` means the relation cannot safely narrow the historical input;
        callers must then use their existing reference reader.
        """
        from aigineering.core.allowance_publication_index import (
            DeclarationPairsRequest,
            PublicationClosureRequest,
            PublicationHeadsRequest,
            PublicationSelection,
            safe_committed_head_relations,
        )

        if (
            self._allowance_checkpoint_mode == "reference"
            or not self._publication_index_tables_present()
        ):
            return None
        owns_snapshot = not self._conn.in_transaction
        if owns_snapshot:
            self._conn.execute("BEGIN")
        try:
            through = self.get_runtime_revision()
            state = self._publication_index_state()
            if state is None:
                return None
            cursor = state["through_revision"]
            if (
                type(state["format_version"]) is not int
                or state["format_version"] != 1
                or type(cursor) is not int
                or cursor < 0
                or cursor > through
                or type(state["has_opaque_heads"]) is not int
                or state["has_opaque_heads"] not in (0, 1)
            ):
                return None
            if cursor:
                basis = self._conn.execute(
                    "SELECT record_id FROM runtime_records WHERE revision=?",
                    (cursor,),
                ).fetchone()
                if basis is None or basis["record_id"] != state["basis_record_id"]:
                    return None
            elif state["basis_record_id"] != "":
                return None
            if state["has_opaque_heads"]:
                return None

            rows = []
            if isinstance(request, PublicationHeadsRequest):
                if not all(
                    isinstance(value, str) and value for value in request.member_ids
                ):
                    return None
                member_ids = tuple(dict.fromkeys(request.member_ids))
                member_id_set = frozenset(member_ids)
                chunk_size = (
                    self._publication_query_chunk_size(1, fixed_parameters=1)
                    if member_ids
                    else 1
                )
                for start in range(0, len(member_ids), chunk_size):
                    chunk = member_ids[start : start + chunk_size]
                    if not chunk:
                        continue
                    placeholders = ",".join("?" for _ in chunk)
                    rows.extend(
                        self._conn.execute(
                            "SELECT DISTINCT h.* FROM publication_head_members m "
                            "INDEXED BY idx_publication_head_members_member "
                            "CROSS JOIN runtime_records h ON h.record_id=m.head_record_id "
                            "WHERE m.member_id IN (" + placeholders + ") "
                            "AND h.record_type='candidate.committed' AND h.revision<=?",
                            (*chunk, cursor),
                        ).fetchall()
                    )
                # Old binaries can append facts without maintaining this index.
                tail = self._conn.execute(
                    "SELECT * FROM runtime_records WHERE record_type='candidate.committed' "
                    "AND revision>? AND revision<=? ORDER BY revision",
                    (cursor, through),
                ).fetchall()
                selected_ids = {str(row["record_id"]) for row in rows}
                for row in tail:
                    head = self._row_to_runtime_record(row)
                    relations = safe_committed_head_relations(head)
                    if relations is None:
                        return None
                    if (
                        not relations[0].isdisjoint(member_id_set)
                        and head.id not in selected_ids
                    ):
                        rows.append(row)
                        selected_ids.add(head.id)
            elif isinstance(request, DeclarationPairsRequest):
                if not all(
                    isinstance(pair, tuple)
                    and len(pair) == 2
                    and all(isinstance(value, str) and value for value in pair)
                    for pair in request.pairs
                ):
                    return None
                chunk_size = (
                    self._publication_query_chunk_size(2, maximum=300)
                    if request.pairs
                    else 1
                )
                for start in range(0, len(request.pairs), chunk_size):
                    pairs = request.pairs[start : start + chunk_size]
                    if not pairs:
                        continue
                    where = " OR ".join(
                        "(json_extract(payload_json,'$.candidate_id')=? AND "
                        "json_extract(payload_json,'$.contract.id')=?)"
                        for _ in pairs
                    )
                    values = tuple(value for pair in pairs for value in pair)
                    rows.extend(
                        self._conn.execute(
                            "SELECT * FROM runtime_records WHERE record_type='contract.declared' "
                            "AND json_type(payload_json,'$.candidate_id')='text' "
                            "AND json_extract(payload_json,'$.candidate_id')<>'' "
                            "AND json_type(payload_json,'$.contract')='object' "
                            "AND json_type(payload_json,'$.contract.id')='text' "
                            "AND json_extract(payload_json,'$.contract.id')<>'' AND "
                            + where
                            + " ORDER BY revision",
                            values,
                        ).fetchall()
                    )
            elif isinstance(request, PublicationClosureRequest):
                if not all(
                    isinstance(value, str) and value for value in request.record_ids
                ):
                    return None
                ids = tuple(dict.fromkeys(request.record_ids))
                chunk_size = (
                    self._publication_query_chunk_size(1, fixed_parameters=1)
                    if ids
                    else 1
                )
                for start in range(0, len(ids), chunk_size):
                    chunk = ids[start : start + chunk_size]
                    if not chunk:
                        continue
                    placeholders = ",".join("?" for _ in chunk)
                    rows.extend(
                        self._conn.execute(
                            "SELECT * FROM runtime_records WHERE record_id IN ("
                            + placeholders
                            + ") AND revision<=?",
                            (*chunk, through),
                        ).fetchall()
                    )
                rows_by_id = {str(row["record_id"]): row for row in rows}
                decoded = [
                    self._row_to_runtime_record(row) for row in rows_by_id.values()
                ]
                head_ids = tuple(
                    record.id
                    for record in decoded
                    if record.record_type == "candidate.committed"
                )
                closure_ids = set(ids)
                for record in decoded:
                    closure_ids.update(record.causal_parents)
                for head_id in head_ids:
                    row = rows_by_id[head_id]
                    head = self._row_to_runtime_record(row)
                    relations = safe_committed_head_relations(head)
                    if relations is None:
                        return None
                    if int(row["revision"]) > cursor:
                        closure_ids.update(relations[0])
                chunk_size = self._publication_query_chunk_size(1) if head_ids else 1
                for start in range(0, len(head_ids), chunk_size):
                    chunk = head_ids[start : start + chunk_size]
                    if not chunk:
                        continue
                    placeholders = ",".join("?" for _ in chunk)
                    rows.extend(
                        self._conn.execute(
                            "SELECT DISTINCT r.* FROM publication_head_members m "
                            "JOIN runtime_records r ON r.record_id=m.member_id "
                            "WHERE m.head_record_id IN (" + placeholders + ")",
                            chunk,
                        ).fetchall()
                    )
                available_ids = rows_by_id.keys() | {
                    str(row["record_id"]) for row in rows
                }
                missing = tuple(closure_ids - available_ids)
                chunk_size = (
                    self._publication_query_chunk_size(1, fixed_parameters=1)
                    if missing
                    else 1
                )
                for start in range(0, len(missing), chunk_size):
                    chunk = missing[start : start + chunk_size]
                    if not chunk:
                        continue
                    placeholders = ",".join("?" for _ in chunk)
                    rows.extend(
                        self._conn.execute(
                            "SELECT * FROM runtime_records WHERE record_id IN ("
                            + placeholders
                            + ") AND revision<=?",
                            (*chunk, through),
                        ).fetchall()
                    )
            else:
                return None

            unique_rows = {str(row["record_id"]): row for row in rows}
            selected = tuple(
                self._row_to_runtime_record(row)
                for row in sorted(
                    unique_rows.values(), key=lambda row: int(row["revision"])
                )
            )
            return PublicationSelection(selected, True)
        finally:
            if owns_snapshot:
                self._conn.execute("ROLLBACK")

    def _materialize_asset_graph_record(self, record: RuntimeRecord) -> None:
        content, definition, assertion = graph_rows_from_record(record)
        if content is not None:
            self._insert_graph_row(
                "asset_contents",
                "content_id",
                str(content["id"]),
                content,
                record.id,
            )
        if definition is not None:
            self._insert_graph_row(
                "asset_definitions",
                "definition_id",
                str(definition["id"]),
                definition,
                record.id,
                actor_id=str(
                    definition.get("actor_id", definition.get("signed_by", "legacy"))
                ),
                key_id=str(definition.get("key_id", "")),
            )
        if assertion is not None:
            self._insert_graph_row(
                "asset_definition_content_assertions",
                "assertion_id",
                str(assertion["id"]),
                assertion,
                record.id,
                definition_id=str(assertion["definition_id"]),
                content_id=str(assertion["content_id"]),
                actor_id=str(
                    assertion.get("actor_id", assertion.get("signed_by", "legacy"))
                ),
                key_id=str(assertion.get("key_id", "")),
            )

    def _insert_graph_row(
        self,
        table: str,
        id_column: str,
        object_id: str,
        payload: dict,
        source_record_id: str,
        **columns: str,
    ) -> None:
        names = (id_column, *columns, "payload_json", "source_record_id")
        values = (
            object_id,
            *columns.values(),
            json.dumps(payload, sort_keys=True),
            source_record_id,
        )
        placeholders = ", ".join("?" for _ in names)
        try:
            self._conn.execute(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})",
                values,
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                f"SELECT payload_json, source_record_id FROM {table} "
                f"WHERE {id_column} = ?",
                (object_id,),
            ).fetchone()
            if row is not None and json.loads(row["payload_json"]) == payload:
                return
            raise ImmutableRecordConflict(table, object_id) from None

    def get_content_objects(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT payload_json FROM asset_contents ORDER BY content_id"
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get_asset_definitions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT payload_json FROM asset_definitions ORDER BY definition_id"
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get_definition_content_assertions(
        self, *, definition_id: str = "", content_id: str = ""
    ) -> list[dict]:
        where: list[str] = []
        values: list[str] = []
        if definition_id:
            where.append("definition_id = ?")
            values.append(definition_id)
        if content_id:
            where.append("content_id = ?")
            values.append(content_id)
        query = "SELECT payload_json FROM asset_definition_content_assertions"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY assertion_id"
        rows = self._conn.execute(query, values).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def rebuild_asset_graph_projection(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM asset_definition_content_assertions")
            self._conn.execute("DELETE FROM asset_definitions")
            self._conn.execute("DELETE FROM asset_contents")
            for _, record in self.scan_runtime_records():
                self._materialize_asset_graph_record(record)

    def get_runtime_record(self, record_id: str) -> RuntimeRecord | None:
        row = self._conn.execute(
            "SELECT * FROM runtime_records WHERE record_id = ?", (record_id,)
        ).fetchone()
        return self._row_to_runtime_record(row) if row is not None else None

    def scan_runtime_records(
        self,
        *,
        after_revision: int = 0,
        through_revision: int | None = None,
        record_type: str | None = None,
    ) -> list[tuple[int, RuntimeRecord]]:
        upper_clause = "" if through_revision is None else " AND revision <= ?"
        values: tuple[object, ...] = (after_revision,)
        if through_revision is not None:
            values += (through_revision,)
        if record_type is None:
            rows = self._conn.execute(
                "SELECT * FROM runtime_records WHERE revision > ?"
                + upper_clause
                + " ORDER BY revision",
                values,
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runtime_records "
                "WHERE revision > ? AND record_type = ?"
                + upper_clause
                + " ORDER BY revision",
                (after_revision, record_type)
                if through_revision is None
                else (after_revision, record_type, through_revision),
            ).fetchall()
        return [
            (int(row["revision"]), self._row_to_runtime_record(row)) for row in rows
        ]

    _ALLOWANCE_RELATIONS = (
        ("allowance.granted", "contract_id"),
        ("allowance.reserved", "source_contract_id"),
        ("allowance.returned", "source_contract_id"),
        ("allowance.returned", "from_contract_id"),
        ("allowance.extinguished", "contract_id"),
    )

    def _allowance_relation_rows(
        self, contract_id: str, after_revision: int, through_revision: int
    ) -> list[tuple[int, RuntimeRecord]]:
        rows_by_revision: dict[int, sqlite3.Row] = {}
        for record_type, field in self._ALLOWANCE_RELATIONS:
            sql = (
                "SELECT * FROM runtime_records WHERE record_type = ? "
                f"AND json_extract(payload_json, '$.{field}') = ? "
                "AND revision > ? AND revision <= ? ORDER BY revision"
            )
            params = (record_type, contract_id, after_revision, through_revision)
            rows = self._conn.execute(sql, params).fetchall()
            rows_by_revision.update({int(row["revision"]): row for row in rows})
        decoded = [
            (revision, self._row_to_runtime_record(row))
            for revision, row in sorted(rows_by_revision.items())
        ]
        return decoded

    @staticmethod
    def _checkpoint_totals_from_row(row) -> RawAllowanceTotals | None:
        if (
            row is None
            or type(row["format_version"]) is not int
            or row["format_version"] != 1
        ):
            return None
        fields = (
            "grant_count",
            "granted_total",
            "reserved_total",
            "returned_in_total",
            "returned_out_total",
            "extinguished_total",
        )
        values: list[int] = []
        for field in fields:
            value = row[field]
            if (
                not isinstance(value, str)
                or re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None
            ):
                return None
            try:
                from decimal import Decimal

                parsed = int(Decimal(value))
            except (ValueError, OverflowError):
                return None
            if _raw_integer_text(parsed) != value:
                return None
            values.append(parsed)
        return RawAllowanceTotals(*values)

    def read_raw_allowance_checkpoint(
        self, contract_id: str
    ) -> RawAllowanceCheckpointRead:
        """Read checkpoint plus exact indexed tail without mutating cache state."""
        if self._allowance_checkpoint_mode == "reference":
            owns_snapshot = not self._conn.in_transaction
            if owns_snapshot:
                self._conn.execute("BEGIN")
            try:
                scanned = self.scan_runtime_records()
                records = [
                    record
                    for _, record in scanned
                    if contract_id in self._allowance_accounts_for_record(record)
                ]
                through = self.get_runtime_revision()
                totals = fold_raw_allowance_records(contract_id, records)
                return RawAllowanceCheckpointRead(
                    "valid" if totals is not None else "opaque",
                    totals,
                    through,
                    len(scanned),
                    len(scanned),
                    len(records),
                )
            finally:
                if owns_snapshot:
                    self._conn.execute("ROLLBACK")
        owns_snapshot = not self._conn.in_transaction
        if owns_snapshot:
            self._conn.execute("BEGIN")
        try:
            tables = {
                str(row[0])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            through = self.get_runtime_revision()
            checkpoint = None
            if "allowance_checkpoints" in tables:
                checkpoint = self._conn.execute(
                    "SELECT * FROM allowance_checkpoints WHERE contract_id = ?",
                    (contract_id,),
                ).fetchone()
            if checkpoint is not None and checkpoint["status"] == "opaque":
                cursor_value = checkpoint["through_revision"]
                cursor = cursor_value if type(cursor_value) is int else -1
                return RawAllowanceCheckpointRead("opaque", None, cursor, 0, 0, 0)
            totals = self._checkpoint_totals_from_row(checkpoint)
            cursor_value = (
                None if checkpoint is None else checkpoint["through_revision"]
            )
            cursor = (
                0
                if checkpoint is None
                else (cursor_value if type(cursor_value) is int else -1)
            )
            if cursor < 0 or cursor > through:
                totals = None
            if checkpoint is not None and (
                checkpoint["status"] != "valid" or totals is None
            ):
                return RawAllowanceCheckpointRead("opaque", None, cursor, 0, 0, 0)
            if checkpoint is not None and cursor:
                basis = self._conn.execute(
                    "SELECT record_id FROM runtime_records WHERE revision = ?",
                    (cursor,),
                ).fetchone()
                if basis is None or basis["record_id"] != checkpoint["basis_record_id"]:
                    return RawAllowanceCheckpointRead("opaque", None, cursor, 0, 0, 0)
            elif checkpoint is not None and checkpoint["basis_record_id"] != "":
                return RawAllowanceCheckpointRead("opaque", None, cursor, 0, 0, 0)
            tail = self._allowance_relation_rows(
                contract_id, cursor if totals is not None else 0, through
            )
            if totals is None:
                totals = RawAllowanceTotals(0, 0, 0, 0, 0, 0)
            tail_totals = fold_raw_allowance_records(
                contract_id, [record for _, record in tail]
            )
            if tail_totals is None:
                return RawAllowanceCheckpointRead(
                    "opaque", None, through, len(tail), len(tail), len(tail)
                )
            totals = RawAllowanceTotals(
                totals.grant_count + tail_totals.grant_count,
                totals.granted_total + tail_totals.granted_total,
                totals.reserved_total + tail_totals.reserved_total,
                totals.returned_in_total + tail_totals.returned_in_total,
                totals.returned_out_total + tail_totals.returned_out_total,
                totals.extinguished_total + tail_totals.extinguished_total,
            )
            return RawAllowanceCheckpointRead(
                "valid", totals, through, len(tail), len(tail), len(tail)
            )
        finally:
            if owns_snapshot:
                self._conn.execute("ROLLBACK")

    def _write_allowance_checkpoint(
        self, contract_id: str, through: int, totals: RawAllowanceTotals | None
    ) -> None:
        if self._allowance_checkpoint_mode != "checkpoint":
            return
        table = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='allowance_checkpoints'"
        ).fetchone()
        if table is None:
            return
        basis = ""
        if through:
            row = self._conn.execute(
                "SELECT record_id FROM runtime_records WHERE revision = ?", (through,)
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError("allowance checkpoint basis is missing")
            basis = str(row["record_id"])
        values = (
            (None,) * 6
            if totals is None
            else tuple(
                _raw_integer_text(value)
                for value in (
                    totals.grant_count,
                    totals.granted_total,
                    totals.reserved_total,
                    totals.returned_in_total,
                    totals.returned_out_total,
                    totals.extinguished_total,
                )
            )
        )
        self._conn.execute(
            "INSERT INTO allowance_checkpoints VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(contract_id) DO UPDATE SET format_version=excluded.format_version, "
            "status=excluded.status, through_revision=excluded.through_revision, "
            "basis_record_id=excluded.basis_record_id, grant_count=excluded.grant_count, "
            "granted_total=excluded.granted_total, reserved_total=excluded.reserved_total, "
            "returned_in_total=excluded.returned_in_total, returned_out_total=excluded.returned_out_total, "
            "extinguished_total=excluded.extinguished_total",
            (
                contract_id,
                "valid" if totals is not None else "opaque",
                through,
                basis,
                *values,
            ),
        )

    def _refresh_allowance_checkpoint_for_record(
        self, record: RuntimeRecord, revision: int
    ) -> None:
        if (
            self._allowance_checkpoint_mode != "checkpoint"
            or record.record_type not in {item[0] for item in self._ALLOWANCE_RELATIONS}
        ):
            return
        payload = record.payload
        affected = set()
        if record.record_type in {"allowance.granted", "allowance.extinguished"}:
            affected.add(str(payload.get("contract_id", "")))
        elif record.record_type == "allowance.reserved":
            affected.add(str(payload.get("source_contract_id", "")))
        elif record.record_type == "allowance.returned":
            affected.add(str(payload.get("source_contract_id", "")))
            affected.add(str(payload.get("from_contract_id", "")))
        affected.discard("")
        for contract_id in sorted(affected):
            table = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='allowance_checkpoints'"
            ).fetchone()
            if table is None:
                return
            row = self._conn.execute(
                "SELECT * FROM allowance_checkpoints WHERE contract_id=?",
                (contract_id,),
            ).fetchone()
            if row is not None and row["status"] != "valid":
                continue
            old = self._checkpoint_totals_from_row(row)
            if row is not None and old is None:
                continue
            if row is not None and type(row["through_revision"]) is not int:
                continue
            cursor = 0 if row is None else int(row["through_revision"])
            if cursor > revision:
                continue
            if row is not None and old is not None and cursor:
                basis = self._conn.execute(
                    "SELECT record_id FROM runtime_records WHERE revision=?", (cursor,)
                ).fetchone()
                if basis is None or basis["record_id"] != row["basis_record_id"]:
                    continue
            elif row is not None and row["basis_record_id"] != "":
                continue
            tail = self._allowance_relation_rows(contract_id, cursor, revision)
            folded = fold_raw_allowance_records(contract_id, [item for _, item in tail])
            if folded is None:
                self._write_allowance_checkpoint(contract_id, revision, None)
                continue
            if old is None:
                self._write_allowance_checkpoint(contract_id, revision, folded)
            else:
                totals = RawAllowanceTotals(
                    old.grant_count + folded.grant_count,
                    old.granted_total + folded.granted_total,
                    old.reserved_total + folded.reserved_total,
                    old.returned_in_total + folded.returned_in_total,
                    old.returned_out_total + folded.returned_out_total,
                    old.extinguished_total + folded.extinguished_total,
                )
                self._write_allowance_checkpoint(contract_id, revision, totals)

    @staticmethod
    def _allowance_accounts_for_record(record: RuntimeRecord) -> set[str]:
        payload = record.payload
        if record.record_type in {"allowance.granted", "allowance.extinguished"}:
            return {str(payload.get("contract_id", ""))} - {""}
        if record.record_type == "allowance.reserved":
            return {str(payload.get("source_contract_id", ""))} - {""}
        if record.record_type == "allowance.returned":
            return {
                str(payload.get("source_contract_id", "")),
                str(payload.get("from_contract_id", "")),
            } - {""}
        return set()

    def rebuild_allowance_checkpoints(
        self,
        records: list[tuple[int, RuntimeRecord]] | None = None,
        through_revision: int | None = None,
    ) -> None:
        """Rebuild only derived allowance rows from one immutable prefix."""
        if self._allowance_checkpoint_mode != "checkpoint":
            return
        table = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='allowance_checkpoints'"
        ).fetchone()
        if table is None:
            return
        owns_transaction = not self._conn.in_transaction
        if owns_transaction:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            if records is None:
                through = self.get_runtime_revision()
                captured = self.scan_runtime_records(through_revision=through)
            else:
                captured = [
                    item
                    for item in records
                    if through_revision is None or item[0] <= through_revision
                ]
                through = (
                    through_revision
                    if through_revision is not None
                    else (captured[-1][0] if captured else 0)
                )
            by_account: dict[str, list[RuntimeRecord]] = {}
            for _, record in captured:
                for account in self._allowance_accounts_for_record(record):
                    by_account.setdefault(account, []).append(record)
            self._conn.execute("DELETE FROM allowance_checkpoints")
            for account, account_records in by_account.items():
                totals = fold_raw_allowance_records(account, account_records)
                self._write_allowance_checkpoint(account, through, totals)
        except Exception:
            if owns_transaction:
                self._conn.execute("ROLLBACK")
            raise
        else:
            if owns_transaction:
                self._conn.execute("COMMIT")

    def audit_allowance_checkpoints(self) -> tuple[str, ...]:
        """Read-only audit of cache rows against exact immutable prefixes."""
        table = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='allowance_checkpoints'"
        ).fetchone()
        if table is None:
            return ()
        owns_snapshot = not self._conn.in_transaction
        if owns_snapshot:
            self._conn.execute("BEGIN")
        try:
            through = self.get_runtime_revision()
            all_records = self.scan_runtime_records(through_revision=through)
            records_by_account: dict[str, list[tuple[int, RuntimeRecord]]] = {}
            for revision, record in all_records:
                for account in self._allowance_accounts_for_record(record):
                    records_by_account.setdefault(account, []).append(
                        (revision, record)
                    )
            issues: list[str] = []
            for row in self._conn.execute(
                "SELECT * FROM allowance_checkpoints ORDER BY contract_id"
            ).fetchall():
                contract_id = str(row["contract_id"])
                cursor_value = row["through_revision"]
                cursor = cursor_value if type(cursor_value) is int else -1
                if (
                    type(row["format_version"]) is not int
                    or row["format_version"] != 1
                    or cursor < 0
                    or cursor > through
                ):
                    issues.append("allowance checkpoint metadata is invalid")
                    continue
                if cursor:
                    basis = self._conn.execute(
                        "SELECT record_id FROM runtime_records WHERE revision=?",
                        (cursor,),
                    ).fetchone()
                    if basis is None or basis["record_id"] != row["basis_record_id"]:
                        issues.append("allowance checkpoint prefix basis is invalid")
                        continue
                elif row["basis_record_id"] != "":
                    issues.append("allowance checkpoint prefix basis is invalid")
                    continue
                if row["status"] == "opaque":
                    if any(
                        row[field] is not None
                        for field in (
                            "grant_count",
                            "granted_total",
                            "reserved_total",
                            "returned_in_total",
                            "returned_out_total",
                            "extinguished_total",
                        )
                    ):
                        issues.append("opaque allowance checkpoint metadata is invalid")
                    continue
                if row["status"] != "valid":
                    issues.append("allowance checkpoint status is invalid")
                    continue
                actual = fold_raw_allowance_records(
                    contract_id,
                    [
                        record
                        for revision, record in records_by_account.get(contract_id, ())
                        if revision <= cursor
                    ],
                )
                expected = self._checkpoint_totals_from_row(row)
                if actual is None or expected is None or actual != expected:
                    issues.append(
                        "allowance checkpoint totals do not match immutable prefix"
                    )
            return tuple(issues)
        finally:
            if owns_snapshot:
                self._conn.execute("ROLLBACK")

    def read_contract_declaration_projection_inputs(self, request):
        """Read exact declaration inputs and relevant runtime facts by index."""
        owns_snapshot = not self._conn.in_transaction
        if owns_snapshot:
            self._conn.execute("BEGIN")
        try:
            return self._read_contract_declaration_projection_inputs(request)
        finally:
            if owns_snapshot:
                self._conn.execute("ROLLBACK")

    def _read_contract_declaration_projection_inputs(self, request):
        from aigineering.core.projection_context import (
            ContractDeclarationProjectionInputs,
        )
        from aigineering.core.allowance_publication_index import AllowanceBalanceInput

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
        assets = tuple(self.get_assets_by_ids(request.asset_ids))
        selected: dict[str, tuple[int, RuntimeRecord]] = {}
        heads: list[RuntimeRecord] = []
        balance_inputs = None

        if request.allowance_source_contract_ids:
            source_ids = tuple(dict.fromkeys(request.allowance_source_contract_ids))
            if self._allowance_checkpoint_mode == "reference":
                self._select_declaration_allowance_reference_rows(source_ids, selected)
            else:
                raw_balances = tuple(
                    self.read_raw_allowance_checkpoint(source_id)
                    for source_id in source_ids
                )
                if all(
                    item.status == "valid" and item.totals is not None
                    for item in raw_balances
                ):
                    balance_inputs = tuple(
                        AllowanceBalanceInput(source_id, item.totals)
                        for source_id, item in zip(
                            source_ids, raw_balances, strict=True
                        )
                    )
                if balance_inputs is None:
                    self._select_declaration_allowance_reference_rows(
                        source_ids, selected
                    )

        if request.include_candidate_replay:
            for record_type in ("candidate.received", "candidate.committed"):
                rows = self._conn.execute(
                    "SELECT * FROM runtime_records WHERE record_type = ? AND "
                    "json_extract(payload_json, '$.candidate_id') = ? ORDER BY revision",
                    (record_type, request.candidate_id),
                ).fetchall()
                for row in rows:
                    record = self._row_to_runtime_record(row)
                    selected[record.id] = (int(row["revision"]), record)
                    if record.record_type == "candidate.committed":
                        heads.append(record)

        member_ids = tuple(
            dict.fromkeys(
                member_id
                for head in heads
                if isinstance(
                    (member_ids_value := head.payload.get("committed_record_ids")),
                    (list, tuple),
                )
                for member_id in member_ids_value
                if isinstance(member_id, str)
            )
        )
        if member_ids:
            parameter_limit = self._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
            chunk_size = min(_ASSET_ID_QUERY_CHUNK_SIZE, parameter_limit)
            if chunk_size < 1:
                raise sqlite3.OperationalError(
                    "SQLite variable limit does not permit replay-member lookup"
                )
            for start in range(0, len(member_ids), chunk_size):
                chunk = member_ids[start : start + chunk_size]
                placeholders = ", ".join("?" for _ in chunk)
                rows = self._conn.execute(
                    "SELECT * FROM runtime_records WHERE record_id IN ("
                    + placeholders
                    + ")",
                    chunk,
                ).fetchall()
                for row in rows:
                    record = self._row_to_runtime_record(row)
                    selected[record.id] = (int(row["revision"]), record)

        runtime_records = tuple(
            record for _, record in sorted(selected.values(), key=lambda item: item[0])
        )
        return ContractDeclarationProjectionInputs(
            contracts=contracts,
            assets=assets,
            runtime_records=runtime_records,
            allowance_balance_inputs=balance_inputs,
        )

    def _select_declaration_allowance_reference_rows(self, source_ids, selected):
        parameter_limit = self._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        relation_count = _PROJECTION_ID_QUERY_RELATION_COUNT
        if parameter_limit <= relation_count:
            raise sqlite3.OperationalError(
                "SQLite variable limit does not permit projection relation lookup"
            )
        chunk_size = min(
            _ASSET_ID_QUERY_CHUNK_SIZE,
            (parameter_limit - relation_count) // relation_count,
        )
        if chunk_size < 1:
            raise sqlite3.OperationalError(
                "SQLite variable limit does not permit projection relation lookup"
            )
        for start in range(0, len(source_ids), chunk_size):
            chunk = source_ids[start : start + chunk_size]
            placeholders = ", ".join("?" for _ in chunk)
            clauses = (
                ("allowance.granted", "contract_id"),
                ("allowance.reserved", "source_contract_id"),
                ("allowance.returned", "source_contract_id"),
                ("allowance.returned", "from_contract_id"),
                ("allowance.extinguished", "contract_id"),
            )
            where = " OR ".join(
                "(record_type = ? AND json_extract(payload_json, '$.%s') IN (%s))"
                % (payload_field, placeholders)
                for _record_type, payload_field in clauses
            )
            values = tuple(
                value
                for record_type, _payload_field in clauses
                for value in (record_type, *chunk)
            )
            rows = self._conn.execute(
                "SELECT * FROM runtime_records WHERE " + where,
                values,
            ).fetchall()
            for row in rows:
                record = self._row_to_runtime_record(row)
                selected[record.id] = (int(row["revision"]), record)

    def get_runtime_revision(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM runtime_records"
        ).fetchone()
        return int(row[0])

    _row_to_runtime_record = staticmethod(runtime_record_from_row)

    # ── Runtime Lifecycle ─────────────────────────────────────────────────

    def upsert_runtime_lifecycle(
        self, runtime_id: str, heartbeat_at: str, state: str
    ) -> None:
        """Insert or update a runtime lifecycle record."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO runtime_lifecycle (runtime_id, heartbeat_at, state, started_at) "
                "VALUES (?, ?, ?, COALESCE((SELECT started_at FROM runtime_lifecycle "
                "WHERE runtime_id = ?), ?)) "
                "ON CONFLICT(runtime_id) DO UPDATE SET "
                "heartbeat_at = excluded.heartbeat_at, state = excluded.state"
                + (", stopped_at = ?" if state == "stopped" else ""),
                (runtime_id, heartbeat_at, state, runtime_id, heartbeat_at)
                + ((heartbeat_at,) if state == "stopped" else ()),
            )

    def get_runtime_lifecycle(self, runtime_id: str) -> dict | None:
        """Return the lifecycle record for *runtime_id*, or None."""
        row = self._conn.execute(
            "SELECT runtime_id, heartbeat_at, state, started_at, stopped_at "
            "FROM runtime_lifecycle WHERE runtime_id = ?",
            (runtime_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_orphaned_runtimes(self, ttl_seconds: int) -> list[dict]:
        """Return lifecycle records for active runtimes whose heartbeat
        is older than *ttl_seconds* seconds from now."""
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        ).isoformat()
        rows = self._conn.execute(
            "SELECT runtime_id, heartbeat_at, state, started_at, stopped_at "
            "FROM runtime_lifecycle "
            "WHERE state = 'active' AND heartbeat_at < ?",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    @property
    def schema_version(self) -> int:
        return current_schema_version(self._conn)

    # ------------------------------------------------------------------
    # Row → dataclass helpers
    # ------------------------------------------------------------------

    _row_to_asset = staticmethod(asset_from_row)
    _row_to_contract = staticmethod(contract_from_row)

    # ------------------------------------------------------------------
    # StoreProtocol: assets
    # ------------------------------------------------------------------

    def add_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        if _is_protected_name(asset.name):
            prefix = matched_reserved_prefix(asset.name) or "?"
            raise ReservedNamespaceError(asset.name, prefix)
        with self._conn:
            self._insert_asset(asset)

    def _add_system_asset(self, asset: Asset) -> None:
        if not asset.signed_by or not verify_asset_seal(asset):
            raise ValueError(
                f"Asset '{asset.id}' rejected — missing or invalid canonical seal "
                f"(signed_by={asset.signed_by!r})"
            )
        with self._conn:
            self._insert_asset(asset)

    def _insert_asset(self, asset: Asset) -> None:
        existing = self.get_asset(asset.id)
        if existing is not None:
            if existing == asset:
                return
            raise ImmutableRecordConflict("asset", asset.id)
        d = asset_to_dict(asset)
        try:
            self._conn.execute(
                """INSERT INTO assets (
                    id, name, content, content_type, created_by,
                    origin, trust_tier, minted_by, source_uri,
                    signed_by, signer_kind, provenance_seal, promptable, disclosure_view,
                    definition_hash, content_hash,
                    keep_flag, tombstoned, tombstoned_at, lineage_id
                ) VALUES (
                    :id, :name, :content, :content_type, :created_by,
                    :origin, :trust_tier, :minted_by, :source_uri,
                    :signed_by, :signer_kind, :provenance_seal, :promptable, :disclosure_view,
                    :definition_hash, :content_hash,
                    :keep_flag, :tombstoned, :tombstoned_at, :lineage_id
                )""",
                {
                    "id": d["id"],
                    "name": d["name"],
                    "content": d["content"],
                    "content_type": d["content_type"],
                    "created_by": d["created_by"],
                    "origin": d["origin"],
                    "trust_tier": d["trust_tier"],
                    "minted_by": d["minted_by"],
                    "source_uri": d["source_uri"],
                    "signed_by": d["signed_by"],
                    "signer_kind": d["signer_kind"],
                    "provenance_seal": d["provenance_seal"],
                    "promptable": int(d["promptable"]),
                    "disclosure_view": d["disclosure_view"],
                    "definition_hash": d["definition_hash"],
                    "content_hash": d["content_hash"],
                    "keep_flag": int(d["keep_flag"]),
                    "tombstoned": int(d["tombstoned"]),
                    "tombstoned_at": d["tombstoned_at"],
                    "lineage_id": d["lineage_id"],
                },
            )
        except sqlite3.IntegrityError:
            existing = self.get_asset(asset.id)
            if existing == asset:
                return
            raise ImmutableRecordConflict("asset", asset.id) from None

    def get_asset(self, asset_id: str) -> Optional[Asset]:
        cur = self._conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,))
        row = cur.fetchone()
        return self._row_to_asset(row) if row else None

    def get_assets_by_ids(self, asset_ids: tuple[str, ...] | list[str]) -> list[Asset]:
        """Read existing Assets for IDs in request order using bounded queries."""
        ordered_ids = tuple(dict.fromkeys(asset_ids))
        if not ordered_ids:
            return []

        found: dict[str, Asset] = {}
        parameter_limit = self._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        if parameter_limit < 1:
            raise sqlite3.OperationalError(
                "SQLite variable limit does not permit Asset ID lookup"
            )
        chunk_size = min(_ASSET_ID_QUERY_CHUNK_SIZE, parameter_limit)
        for start in range(0, len(ordered_ids), chunk_size):
            chunk = ordered_ids[start : start + chunk_size]
            placeholders = ", ".join("?" for _ in chunk)
            cur = self._conn.execute(
                f"SELECT * FROM assets WHERE id IN ({placeholders})", chunk
            )
            for row in cur.fetchall():
                asset = self._row_to_asset(row)
                found[asset.id] = asset
        return [found[asset_id] for asset_id in ordered_ids if asset_id in found]

    def get_assets_by_name(self, name: str) -> list[Asset]:
        cur = self._conn.execute("SELECT * FROM assets WHERE name = ?", (name,))
        return [self._row_to_asset(row) for row in cur.fetchall()]

    def has_asset_named(self, name: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM assets WHERE name = ? LIMIT 1", (name,))
        return cur.fetchone() is not None

    def get_all_assets(self) -> list[Asset]:
        cur = self._conn.execute("SELECT * FROM assets")
        return [self._row_to_asset(row) for row in cur.fetchall()]

    def get_assets_by_contract(self, contract_id: str) -> list[Asset]:
        cur = self._conn.execute(
            "SELECT * FROM assets WHERE created_by = ?", (contract_id,)
        )
        return [self._row_to_asset(row) for row in cur.fetchall()]

    def get_assets_by_definition(self, def_hash: str) -> list[Asset]:
        cur = self._conn.execute(
            "SELECT * FROM assets WHERE definition_hash = ?", (def_hash,)
        )
        return [self._row_to_asset(row) for row in cur.fetchall()]

    def get_latest_asset(self, def_hash: str) -> Optional[Asset]:
        cur = self._conn.execute(
            "SELECT * FROM assets WHERE definition_hash = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (def_hash,),
        )
        row = cur.fetchone()
        return self._row_to_asset(row) if row else None

    # ------------------------------------------------------------------
    # StoreProtocol: contracts
    # ------------------------------------------------------------------

    def _insert_contract(self, contract: Contract) -> None:
        """Insert an immutable contract row + derived index rows."""
        validate_contract_identity(contract)
        existing = self.get_contract(contract.id)
        if existing is not None:
            if existing == contract:
                return
            raise ImmutableRecordConflict("contract", contract.id)
        d = contract_to_dict(contract)
        try:
            self._conn.execute(
                """INSERT INTO contracts (
                id, parent_id, name, description,
                inputs, outputs, activation, budget,
                tool_scope, labels, context_asset_ids, worker_capabilities, worker_pools,
                delegation_capabilities, delegation_pools,
                origin, minting_authority, sensitive_input_policy, acceptance_policy
            ) VALUES (
                :id, :parent_id, :name, :description,
                :inputs, :outputs, :activation, :budget,
                :tool_scope, :labels, :context_asset_ids, :worker_capabilities, :worker_pools,
                :delegation_capabilities, :delegation_pools,
                :origin, :minting_authority, :sensitive_input_policy, :acceptance_policy
            )""",
                {
                    "id": d["id"],
                    "parent_id": d["parent_id"],
                    "name": d["name"],
                    "description": d["description"],
                    "inputs": json.dumps(list(d["inputs"])),
                    "outputs": json.dumps(list(d["outputs"])),
                    "activation": d["activation"],
                    "budget": d["budget"],
                    "tool_scope": json.dumps(list(d["tool_scope"])),
                    "labels": json.dumps(list(d["labels"])),
                    "context_asset_ids": json.dumps(list(d["context_asset_ids"])),
                    "worker_capabilities": json.dumps(list(d["worker_capabilities"])),
                    "worker_pools": json.dumps(list(d["worker_pools"])),
                    "delegation_capabilities": json.dumps(
                        list(d["delegation_capabilities"])
                    ),
                    "delegation_pools": json.dumps(list(d["delegation_pools"])),
                    "origin": d["origin"],
                    "minting_authority": json.dumps(list(d["minting_authority"])),
                    "sensitive_input_policy": (
                        json.dumps(d["sensitive_input_policy"], sort_keys=True)
                        if d["sensitive_input_policy"] is not None
                        else None
                    ),
                    "acceptance_policy": (
                        json.dumps(d["acceptance_policy"], sort_keys=True)
                        if d["acceptance_policy"] is not None
                        else None
                    ),
                },
            )
        except sqlite3.IntegrityError:
            existing = self.get_contract(contract.id)
            if existing == contract:
                return
            raise ImmutableRecordConflict("contract", contract.id) from None
        self._conn.execute(
            "DELETE FROM contract_activation_refs WHERE contract_id = ?",
            (contract.id,),
        )
        for name in activation_names(contract.activation):
            self._conn.execute(
                "INSERT INTO contract_activation_refs (contract_id, asset_name) "
                "VALUES (?, ?)",
                (contract.id, name),
            )
        self._conn.execute(
            "DELETE FROM contract_declared_outputs WHERE contract_id = ?",
            (contract.id,),
        )
        for name in contract.outputs:
            self._conn.execute(
                "INSERT INTO contract_declared_outputs (contract_id, output_name) "
                "VALUES (?, ?)",
                (contract.id, name),
            )

    def add_contract(self, contract: Contract) -> None:
        with self._conn:
            self._insert_contract(contract)

    def get_contract(self, contract_id: str) -> Optional[Contract]:
        cur = self._conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,))
        row = cur.fetchone()
        return self._row_to_contract(row) if row else None

    def get_all_contracts(self) -> list[Contract]:
        cur = self._conn.execute("SELECT * FROM contracts")
        return [self._row_to_contract(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Worker registration / routing control plane
    # ------------------------------------------------------------------

    def register_worker(self, registration: WorkerRegistration) -> None:
        """Append one registration version and update its derived current view."""
        self.append_runtime_record(worker_registration_record(registration))

    def _upsert_worker_registration(self, registration: WorkerRegistration) -> None:
        self._conn.execute(
            """INSERT INTO worker_registrations (
                worker_id, actor_id, key_id, capabilities, pools, profile_id,
                capacity, enabled, version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                actor_id = excluded.actor_id,
                key_id = excluded.key_id,
                capabilities = excluded.capabilities,
                pools = excluded.pools,
                profile_id = excluded.profile_id,
                capacity = excluded.capacity,
                enabled = excluded.enabled,
                version = excluded.version,
                updated_at = excluded.updated_at""",
            (
                registration.worker_id,
                registration.actor_id,
                registration.key_id,
                json.dumps(list(registration.capabilities)),
                json.dumps(list(registration.pools)),
                registration.profile_id,
                registration.capacity,
                int(registration.enabled),
                registration.version,
                now_iso(),
            ),
        )

    def rebuild_worker_registration_projection(self) -> None:
        """Rebuild the mutable routing view from immutable registration facts."""
        with self._conn:
            self._conn.execute("DELETE FROM worker_registrations")
            for _, record in self.scan_runtime_records(record_type="worker.registered"):
                registration = registration_from_record(record)
                self._conn.execute(
                    """INSERT INTO worker_registrations (
                        worker_id, actor_id, key_id, capabilities, pools,
                        profile_id, capacity, enabled, version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(worker_id) DO UPDATE SET
                        actor_id = excluded.actor_id,
                        key_id = excluded.key_id,
                        capabilities = excluded.capabilities,
                        pools = excluded.pools,
                        profile_id = excluded.profile_id,
                        capacity = excluded.capacity,
                        enabled = excluded.enabled,
                        version = excluded.version,
                        updated_at = excluded.updated_at""",
                    (
                        registration.worker_id,
                        registration.actor_id,
                        registration.key_id,
                        json.dumps(list(registration.capabilities)),
                        json.dumps(list(registration.pools)),
                        registration.profile_id,
                        registration.capacity,
                        int(registration.enabled),
                        registration.version,
                        record.recorded_at,
                    ),
                )

    def get_worker_registration(self, worker_id: str) -> WorkerRegistration | None:
        row = self._conn.execute(
            "SELECT * FROM worker_registrations WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        return self._row_to_worker_registration(row) if row is not None else None

    def get_worker_registrations(self) -> list[WorkerRegistration]:
        rows = self._conn.execute(
            "SELECT * FROM worker_registrations ORDER BY worker_id"
        ).fetchall()
        return [self._row_to_worker_registration(row) for row in rows]

    def get_claim_history_contract_ids(self) -> frozenset[str]:
        """Return all contract IDs with any durable worker-claim row."""
        return frozenset(
            str(row[0])
            for row in self._conn.execute(
                "SELECT DISTINCT contract_id FROM worker_claims"
            ).fetchall()
        )

    def _row_to_worker_registration(self, row: sqlite3.Row) -> WorkerRegistration:
        active_claims = self._conn.execute(
            "SELECT COUNT(*) FROM worker_claims WHERE worker_id = ? AND status = 'active'",
            (row["worker_id"],),
        ).fetchone()[0]
        return WorkerRegistration(
            worker_id=row["worker_id"],
            actor_id=row["actor_id"],
            key_id=row["key_id"],
            capabilities=tuple(json.loads(row["capabilities"])),
            pools=tuple(json.loads(row["pools"])),
            profile_id=row["profile_id"],
            capacity=row["capacity"],
            active_claims=active_claims,
            enabled=bool(row["enabled"]),
            version=row["version"],
        )

    # ------------------------------------------------------------------
    # Activation / declared-output indexes
    # ------------------------------------------------------------------

    def register_activation_refs(self, contract_id: str, asset_names: set[str]) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM contract_activation_refs WHERE contract_id = ?",
                (contract_id,),
            )
            for name in asset_names:
                self._conn.execute(
                    "INSERT INTO contract_activation_refs (contract_id, asset_name) "
                    "VALUES (?, ?)",
                    (contract_id, name),
                )

    def unregister_activation_refs(self, contract_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM contract_activation_refs WHERE contract_id = ?",
                (contract_id,),
            )

    def register_declared_outputs(
        self, contract_id: str, output_names: set[str]
    ) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM contract_declared_outputs WHERE contract_id = ?",
                (contract_id,),
            )
            for name in output_names:
                self._conn.execute(
                    "INSERT INTO contract_declared_outputs (contract_id, output_name) "
                    "VALUES (?, ?)",
                    (contract_id, name),
                )

    def unregister_declared_outputs(self, contract_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM contract_declared_outputs WHERE contract_id = ?",
                (contract_id,),
            )

    def get_contracts_waiting_for(self, asset_name: str) -> list[str]:
        cur = self._conn.execute(
            "SELECT contract_id FROM contract_activation_refs WHERE asset_name = ?",
            (asset_name,),
        )
        return [row[0] for row in cur.fetchall()]

    def get_contracts_declaring_output(self, asset_name: str) -> list[str]:
        cur = self._conn.execute(
            "SELECT contract_id FROM contract_declared_outputs WHERE output_name = ?",
            (asset_name,),
        )
        return [row[0] for row in cur.fetchall()]

    def rebuild_projection_indexes(self) -> None:
        """Atomically rebuild derived indexes from immutable contracts."""
        with self._conn:
            self._conn.execute("DELETE FROM contract_activation_refs")
            self._conn.execute("DELETE FROM contract_declared_outputs")
            for contract in self.get_all_contracts():
                for name in activation_names(contract.activation):
                    self._conn.execute(
                        "INSERT INTO contract_activation_refs "
                        "(contract_id, asset_name) VALUES (?, ?)",
                        (contract.id, name),
                    )
                for name in contract.outputs:
                    self._conn.execute(
                        "INSERT INTO contract_declared_outputs "
                        "(contract_id, output_name) VALUES (?, ?)",
                        (contract.id, name),
                    )

    def projection_index_digest(self) -> str:
        activation_rows = [
            (row[0], row[1])
            for row in self._conn.execute(
                "SELECT contract_id, asset_name FROM contract_activation_refs"
            ).fetchall()
        ]
        output_rows = [
            (row[0], row[1])
            for row in self._conn.execute(
                "SELECT contract_id, output_name FROM contract_declared_outputs"
            ).fetchall()
        ]
        return _projection_index_digest(activation_rows, output_rows)

    def runtime_materialization_digest(self) -> str:
        """Hash semantic materialized views, excluding cache timestamps."""
        checkpoint_issues = self.audit_allowance_checkpoints()
        if checkpoint_issues:
            raise RuntimeError("allowance checkpoint integrity failure")
        publication_issues = self.audit_publication_index()
        if publication_issues:
            raise RuntimeError("publication index integrity failure")
        registrations = [
            {
                "worker_id": registration.worker_id,
                "actor_id": registration.actor_id,
                "key_id": registration.key_id,
                "capabilities": registration.capabilities,
                "pools": registration.pools,
                "profile_id": registration.profile_id,
                "capacity": registration.capacity,
                "enabled": registration.enabled,
                "version": registration.version,
            }
            for registration in self.get_worker_registrations()
        ]
        claim_rows = self._conn.execute(
            "SELECT claim_id, contract_id, worker_id, lease_until, status, "
            "package_id, epoch FROM worker_claims ORDER BY contract_id, epoch"
        ).fetchall()
        idempotency_rows = self._conn.execute(
            "SELECT contract_id, idempotency_key, result_json "
            "FROM idempotency_records ORDER BY contract_id, idempotency_key"
        ).fetchall()
        replacement_claims = [
            replacement_claim_payload(self._row_to_replacement_claim(row))
            for row in self._conn.execute("SELECT * FROM claims ORDER BY id").fetchall()
        ]
        payload = {
            "asset_graph": {
                "assertions": self.get_definition_content_assertions(),
                "contents": self.get_content_objects(),
                "definitions": self.get_asset_definitions(),
            },
            "assets": sorted(
                (asset_to_dict(asset) for asset in self.get_all_assets()),
                key=lambda row: row["id"],
            ),
            "claims": [dict(row) for row in claim_rows],
            "contracts": sorted(
                (contract_to_dict(contract) for contract in self.get_all_contracts()),
                key=lambda row: row["id"],
            ),
            "idempotency": [
                {
                    "contract_id": row["contract_id"],
                    "idempotency_key": row["idempotency_key"],
                    "result": json.loads(row["result_json"]),
                }
                for row in idempotency_rows
            ],
            "projection_indexes": self.projection_index_digest(),
            "registrations": registrations,
            "replacement_claims": replacement_claims,
            "trace": [
                trace_entry_to_dict(entry)
                for entry in sorted(self.get_all(), key=lambda item: item.id)
            ],
        }
        return compute_content_hash(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )

    def rebuild_runtime_materializations(self, *, strict: bool = True) -> str:
        """Rebuild Contract/Asset/Trace and control heads from RuntimeRecords."""
        records = self.scan_runtime_records()
        contract_payloads = {
            record.payload["contract"]["id"]: dict(record.payload["contract"])
            for _, record in records
            if record.record_type == "contract.declared"
        }
        asset_payloads = {
            record.payload["asset"]["id"]: dict(record.payload["asset"])
            for _, record in records
            if record.record_type == "asset.committed"
        }
        trace_payloads = {}
        for _, record in records:
            if record.record_type != "trace.recorded":
                continue
            payload = dict(record.payload["trace"])
            existing = trace_payloads.setdefault(payload["id"], payload)
            # Live insertion retains the first timestamp for an idempotent
            # semantic trace. Concurrent recovery may record it again with a
            # later timestamp; replay must use the same first-write rule.
            if trace_effective_payload(
                trace_entry_from_dict(existing)
            ) != trace_effective_payload(trace_entry_from_dict(payload)):
                raise ImmutableRecordConflict("trace event", payload["id"])
        idempotency_payloads = {
            (str(record.payload["contract_id"]), str(record.payload["key"])): (
                runtime_record_effective_payload(record)["payload"]["result"]
            )
            for _, record in records
            if record.record_type == "idempotency.bound"
        }
        replacement_payloads = {
            str(record.payload["claim"]["id"]): runtime_record_effective_payload(
                record
            )["payload"]["claim"]
            for _, record in records
            if record.record_type == "replacement.claimed"
        }
        if strict:
            current_idempotency = {
                (row["contract_id"], row["idempotency_key"])
                for row in self._conn.execute(
                    "SELECT contract_id, idempotency_key FROM idempotency_records"
                ).fetchall()
            }
            missing = {
                "assets": sorted(
                    {asset.id for asset in self.get_all_assets()}
                    - asset_payloads.keys()
                ),
                "contracts": sorted(
                    {contract.id for contract in self.get_all_contracts()}
                    - contract_payloads.keys()
                ),
                "idempotency": sorted(
                    current_idempotency - idempotency_payloads.keys()
                ),
                "replacement_claims": sorted(
                    {
                        row["id"]
                        for row in self._conn.execute(
                            "SELECT id FROM claims"
                        ).fetchall()
                    }
                    - replacement_payloads.keys()
                ),
                "trace": sorted(
                    {entry.id for entry in self.get_all()} - trace_payloads.keys()
                ),
            }
            if any(missing.values()):
                raise RuntimeError(
                    "runtime materialization contains facts absent from immutable log: "
                    + json.dumps(missing, sort_keys=True)
                )

        with self._conn:
            self._conn.execute("DELETE FROM contract_activation_refs")
            self._conn.execute("DELETE FROM contract_declared_outputs")
            self._conn.execute("DELETE FROM trace_events")
            self._conn.execute("DELETE FROM idempotency_records")
            self._conn.execute("DELETE FROM claims")
            self._conn.execute("DELETE FROM assets")
            self._conn.execute("DELETE FROM contracts")
            for payload in contract_payloads.values():
                self._insert_contract(Contract(**payload))
            for payload in asset_payloads.values():
                asset = Asset(**payload)
                if not asset.signed_by or not verify_asset_seal(asset):
                    raise ValueError(
                        f"runtime fact for Asset '{asset.id}' has an invalid seal"
                    )
                self._insert_asset(asset)
            for payload in trace_payloads.values():
                self._insert_trace_entry(trace_entry_from_dict(payload))
            for (contract_id, key), result in idempotency_payloads.items():
                self.set_idempotency(contract_id, key, result)
            for payload in replacement_payloads.values():
                self._insert_replacement_claim(ReplacementClaim(**payload))
            self.rebuild_allowance_checkpoints(
                records, max((revision for revision, _ in records), default=0)
            )
            self.rebuild_publication_index()
        self.rebuild_worker_registration_projection()
        self.rebuild_claim_projection()
        self.rebuild_asset_graph_projection()
        return self.runtime_materialization_digest()

    # ------------------------------------------------------------------
    # Trace operations (G3, 040 C3)
    # ------------------------------------------------------------------

    def append_trace_entry(self, entry) -> None:
        """Insert a TraceEntry into the trace_events table."""
        with self._conn:
            self._insert_trace_entry(entry)
            self._insert_runtime_record(
                create_runtime_record(
                    "trace.recorded",
                    {"trace": trace_entry_to_dict(entry)},
                )
            )

    def append(self, entry: TraceEntry) -> None:
        """TraceStoreProtocol-compatible append."""
        self.append_trace_entry(entry)

    def new_entry(
        self, contract_id: str, event_type: str, **kwargs: object
    ) -> TraceEntry:
        """TraceStoreProtocol-compatible helper."""
        from aigineering.core.trace import create_entry

        parent_id = kwargs.get("parent_id")
        if parent_id is None:
            existing = self.get_trace_events(contract_id)
            if existing:
                kwargs["parent_id"] = existing[-1].id
        entry = create_entry(
            contract_id=contract_id,
            event_type=event_type,
            sequence=len(self.get_trace_events()),
            **kwargs,  # type: ignore[arg-type]
        )
        self.append_trace_entry(entry)
        return entry

    def _insert_trace_entry(self, entry: TraceEntry) -> None:
        row = self._conn.execute(
            "SELECT * FROM trace_events WHERE id = ?", (entry.id,)
        ).fetchone()
        if row is not None:
            existing = self._row_to_trace_entry(row)
            if trace_effective_payload(existing) == trace_effective_payload(entry):
                return
            raise ImmutableRecordConflict("trace event", entry.id)
        d = trace_entry_to_dict(entry)
        try:
            self._conn.execute(
                """INSERT INTO trace_events (
                    id, parent_id, contract_id, event_type,
                    disclosed_assets, accepted_fragments, accepted_asset_names,
                    rejected_fragments, worker_id, candidate_raw,
                    authority_policy, authority_result, budget_remaining,
                    relation_type, relation_target, timestamp, usage_metadata
                ) VALUES (
                    :id, :parent_id, :contract_id, :event_type,
                    :disclosed_assets, :accepted_fragments, :accepted_asset_names,
                    :rejected_fragments, :worker_id, :candidate_raw,
                    :authority_policy, :authority_result, :budget_remaining,
                    :relation_type, :relation_target, :timestamp, :usage_metadata
                )""",
                {
                    k: json.dumps(
                        v,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if isinstance(v, (list, dict, tuple))
                    else v
                    for k, v in d.items()
                },
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                "SELECT * FROM trace_events WHERE id = ?", (entry.id,)
            ).fetchone()
            if row is not None and trace_effective_payload(
                self._row_to_trace_entry(row)
            ) == trace_effective_payload(entry):
                return
            raise ImmutableRecordConflict("trace event", entry.id) from None

    def get_trace_events(self, contract_id: str | None = None) -> list:
        """Return trace entries, optionally filtered by contract_id."""
        if contract_id is not None:
            cur = self._conn.execute(
                "SELECT * FROM trace_events WHERE contract_id = ? ORDER BY rowid",
                (contract_id,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM trace_events ORDER BY rowid")
        return [self._row_to_trace_entry(row) for row in cur.fetchall()]

    def get_by_contract(self, contract_id: str) -> list[TraceEntry]:
        return self.get_trace_events(contract_id)

    def get_by_event_type(self, event_type: str) -> list[TraceEntry]:
        cur = self._conn.execute(
            "SELECT * FROM trace_events WHERE event_type = ? ORDER BY rowid",
            (event_type,),
        )
        return [self._row_to_trace_entry(row) for row in cur.fetchall()]

    def get_by_relation_target(self, relation_target: str) -> list[TraceEntry]:
        cur = self._conn.execute(
            "SELECT * FROM trace_events WHERE relation_target = ? ORDER BY rowid",
            (relation_target,),
        )
        return [self._row_to_trace_entry(row) for row in cur.fetchall()]

    def get_all(self) -> list[TraceEntry]:
        return self.get_trace_events()

    def get_reverse_lineage(self, asset_id: str) -> list[TraceEntry]:
        return [
            entry
            for entry in self.get_trace_events()
            if entry_references_asset(entry, asset_id)
        ]

    def _row_to_trace_entry(self, row: sqlite3.Row):
        """Convert a trace_events row to a TraceEntry."""
        from aigineering.protocol.types import TraceEntry
        import json as _json

        usage_raw = row["usage_metadata"] if "usage_metadata" in row.keys() else None
        usage = _json.loads(usage_raw) if usage_raw else None

        return TraceEntry(
            id=row["id"],
            parent_id=row["parent_id"],
            contract_id=row["contract_id"],
            event_type=row["event_type"],
            disclosed_assets=_json.loads(row["disclosed_assets"] or "[]"),
            accepted_fragments=_json.loads(row["accepted_fragments"] or "[]"),
            accepted_asset_names=_json.loads(row["accepted_asset_names"] or "[]"),
            rejected_fragments=_json.loads(row["rejected_fragments"] or "[]"),
            worker_id=row["worker_id"],
            candidate_raw=row["candidate_raw"],
            authority_policy=row["authority_policy"],
            authority_result=row["authority_result"],
            budget_remaining=row["budget_remaining"],
            relation_type=row["relation_type"],
            relation_target=row["relation_target"],
            timestamp=row["timestamp"],
            usage_metadata=MappingProxyType(usage) if isinstance(usage, dict) else None,
        )

    # ------------------------------------------------------------------
    # Claim persistence (040 C4, G8)
    # ------------------------------------------------------------------

    def persist_claim(
        self,
        claim_id: str,
        contract_id: str,
        worker_id: str,
        lease_until: str,
        status: str = "active",
        package_id: str = "",
        epoch: int = 1,
    ) -> None:
        """Import an immutable worker claim and append its lifecycle facts.

        This compatibility entry point is intentionally insert-only. Runtime
        state transitions use the explicit lifecycle methods below; callers
        cannot rewrite a previously observed claim in place.
        """
        if status not in {"active", "released", "submitted"}:
            raise ValueError(f"unsupported claim status: {status!r}")
        now = now_iso()
        with self._conn:
            existing = self._conn.execute(
                "SELECT claim_id, contract_id, worker_id, lease_until, status, "
                "package_id, epoch FROM worker_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            expected = (
                claim_id,
                contract_id,
                worker_id,
                lease_until,
                status,
                package_id,
                epoch,
            )
            if existing is not None:
                observed = tuple(existing[key] for key in existing.keys())
                if observed == expected:
                    return
                raise ImmutableRecordConflict("worker claim", claim_id)
            self._conn.execute(
                """INSERT INTO worker_claims (
                    claim_id, contract_id, worker_id, lease_until, status,
                    package_id, epoch, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    claim_id,
                    contract_id,
                    worker_id,
                    lease_until,
                    status,
                    package_id,
                    epoch,
                    now,
                    now,
                ),
            )
            granted = create_runtime_record(
                "claim.granted",
                {
                    "claim_id": claim_id,
                    "contract_id": contract_id,
                    "epoch": epoch,
                    "lease_until": lease_until,
                    "package_id": package_id,
                    "worker_id": worker_id,
                },
                recorded_at=now,
            )
            self._insert_runtime_record(granted)
            if status != "active":
                self._insert_runtime_record(
                    create_runtime_record(
                        f"claim.{status}",
                        {
                            "claim_id": claim_id,
                            "contract_id": contract_id,
                            "epoch": epoch,
                            "package_id": package_id,
                            "worker_id": worker_id,
                        },
                        causal_parents=[granted.id],
                        recorded_at=now,
                    )
                )

    def mark_claim_released(self, claim_id: str) -> None:
        """Append a release fact and update the derived claim head."""
        with self._conn:
            row = self._conn.execute(
                "SELECT contract_id, worker_id, epoch, package_id, lease_until "
                "FROM worker_claims WHERE claim_id = ? AND status = 'active'",
                (claim_id,),
            ).fetchone()
            if row is None:
                return
            recorded_at = now_iso()
            self._conn.execute(
                "UPDATE worker_claims SET status = 'released', updated_at = ? "
                "WHERE claim_id = ? AND status = 'active'",
                (recorded_at, claim_id),
            )
            self._insert_runtime_record(
                create_runtime_record(
                    "claim.released",
                    {
                        "claim_id": claim_id,
                        "contract_id": row["contract_id"],
                        "epoch": int(row["epoch"]),
                        "package_id": row["package_id"],
                        "worker_id": row["worker_id"],
                    },
                    causal_parents=[self._claim_granted_record_id(claim_id)],
                    recorded_at=recorded_at,
                )
            )

    def get_claim(self, contract_id: str) -> dict | None:
        """Return the active claim for *contract_id*, or None."""
        cur = self._conn.execute(
            "SELECT claim_id, contract_id, worker_id, lease_until, status, package_id, epoch "
            "FROM worker_claims WHERE contract_id = ? "
            "ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END, rowid DESC LIMIT 1",
            (contract_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "claim_id": row["claim_id"],
            "contract_id": row["contract_id"],
            "worker_id": row["worker_id"],
            "lease_until": row["lease_until"],
            "status": row["status"],
            "package_id": row["package_id"],
            "epoch": int(row["epoch"]),
        }

    def claim_contract(
        self,
        contract_id: str,
        worker_id: str,
        lease_seconds: int = 60,
        package_id: str = "",
        expected_registration_version: str = "",
        runtime_records: tuple[RuntimeRecord, ...] = (),
        *,
        claim_candidate=None,
    ) -> dict | None:
        """Atomically arbitrate contract exclusivity and registered capacity."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claim_id = f"lease:{compute_content_hash(f'{contract_id}|{worker_id}|{now.isoformat()}')}"
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            terminal = self._conn.execute(
                "SELECT 1 FROM runtime_records WHERE record_type = "
                "'lifecycle.terminal' AND "
                "json_extract(payload_json, '$.contract_id') = ? LIMIT 1",
                (contract_id,),
            ).fetchone()
            if terminal is not None:
                self._conn.rollback()
                return None
            if open_claim_holds(
                (record for _, record in self.scan_runtime_records()),
                contract_id=contract_id,
            ):
                self._conn.rollback()
                return None
            from aigineering.core.claim_constraints import claim_constraint_blockers

            if claim_constraint_blockers(
                (record for _, record in self.scan_runtime_records()),
                contract_id=contract_id,
            ):
                self._conn.rollback()
                return None
            existing = self._conn.execute(
                "SELECT claim_id, status FROM worker_claims "
                "WHERE contract_id = ? "
                "ORDER BY epoch DESC LIMIT 1",
                (contract_id,),
            ).fetchone()
            if existing is not None:
                self._conn.rollback()
                return None

            registration = self._conn.execute(
                "SELECT enabled, capacity, version FROM worker_registrations "
                "WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            from aigineering.core.claim_assignments import (
                assignment_context_from_projection,
                effective_assignment,
            )
            from aigineering.core.projection_context import (
                EffectProjectionContext,
            )

            assignment_context = assignment_context_from_projection(
                EffectProjectionContext(
                    runtime_records=tuple(
                        record for _, record in self.scan_runtime_records()
                    )
                )
            )
            assignment = effective_assignment(
                assignment_context, contract_id=contract_id
            )
            if assignment is not None:
                current_registration = self.get_worker_registration(worker_id)
                contract = self.get_contract(contract_id)
                from aigineering.core.worker_routing import is_compatible

                if (
                    assignment.payload.get("worker_id") != worker_id
                    or current_registration is None
                    or not current_registration.enabled
                    or contract is None
                    or not is_compatible(contract, current_registration)
                ):
                    self._conn.rollback()
                    return None
            candidate_records = ()
            if claim_candidate is not None:
                if registration is None:
                    self._conn.rollback()
                    return None
                from aigineering.core.worker_coordination import (
                    same_worker_command_records,
                    validate_worker_claim_candidate,
                )
                from aigineering.core.actor_facts import load_effective_actor_keys
                from aigineering.core.domain import load_genesis

                current_registration = self.get_worker_registration(worker_id)
                genesis = load_genesis(self)
                actor_keys = load_effective_actor_keys(self, genesis)
                candidate_records = validate_worker_claim_candidate(
                    claim_candidate,
                    genesis=genesis,
                    actor_keys=actor_keys,
                    registration=current_registration,
                    worker_id=worker_id,
                    requested_contract_id=contract_id,
                    lease_seconds=lease_seconds,
                    expected_registration_version=expected_registration_version,
                )
                if runtime_records and not same_worker_command_records(
                    tuple(runtime_records), candidate_records
                ):
                    self._conn.rollback()
                    return None
            elif runtime_records:
                self._conn.rollback()
                raise ValueError(
                    "claim runtime_records require their signed claim_candidate"
                )
            else:
                candidate_records = ()
            if registration is not None:
                active_claims = self._conn.execute(
                    "SELECT COUNT(*) FROM worker_claims "
                    "WHERE worker_id = ? AND status = 'active'",
                    (worker_id,),
                ).fetchone()[0]
                if (
                    not bool(registration["enabled"])
                    or active_claims >= int(registration["capacity"])
                    or (
                        expected_registration_version
                        and registration["version"] != expected_registration_version
                    )
                ):
                    self._conn.rollback()
                    return None

            epoch = int(
                self._conn.execute(
                    "SELECT COALESCE(MAX(epoch), 0) + 1 FROM worker_claims "
                    "WHERE contract_id = ?",
                    (contract_id,),
                ).fetchone()[0]
            )
            timestamp = now_iso()
            if any(
                self.get_runtime_record(record.id) is not None
                for record in candidate_records
            ):
                self._conn.rollback()
                return None
            for record in candidate_records:
                self._insert_runtime_record(record)
            claim_request = next(
                (
                    record
                    for record in candidate_records
                    if record.record_type == "worker.claim.requested"
                ),
                None,
            )
            self._conn.execute(
                """INSERT INTO worker_claims (
                    claim_id, contract_id, worker_id, lease_until, status,
                    package_id, epoch, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                (
                    claim_id,
                    contract_id,
                    worker_id,
                    lease_until,
                    package_id,
                    epoch,
                    timestamp,
                    timestamp,
                ),
            )
            self._insert_runtime_record(
                create_runtime_record(
                    "claim.granted",
                    {
                        "claim_id": claim_id,
                        "contract_id": contract_id,
                        "epoch": epoch,
                        "lease_until": lease_until,
                        "package_id": package_id,
                        "worker_id": worker_id,
                    },
                    causal_parents=(claim_request.id,) if claim_request else (),
                )
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return None
        except sqlite3.OperationalError as exc:
            self._conn.rollback()
            if _is_sqlite_contention(exc):
                return None
            raise
        except Exception:
            self._conn.rollback()
            raise
        return self.get_claim(contract_id)

    def renew_claim(
        self,
        claim_id: str,
        epoch: int,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        runtime_records: tuple[RuntimeRecord, ...] = (),
        claim_candidate=None,
    ) -> dict | None:
        """Renew an active claim only when all fencing identities still match."""
        from datetime import datetime, timedelta, timezone

        if epoch < 1 or lease_seconds < 1:
            return None
        now = datetime.now(timezone.utc)
        deadline = (now + timedelta(seconds=lease_seconds)).isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT contract_id, worker_id, epoch, package_id, lease_until "
                "FROM worker_claims WHERE claim_id = ? AND epoch = ? "
                "AND worker_id = ? AND status = 'active' AND lease_until >= ?",
                (claim_id, epoch, worker_id, now.isoformat()),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return None

            candidate_records = ()
            if claim_candidate is not None:
                registration = self.get_worker_registration(worker_id)
                if registration is None:
                    self._conn.rollback()
                    return None
                from aigineering.core.actor_facts import load_effective_actor_keys
                from aigineering.core.domain import load_genesis
                from aigineering.core.worker_coordination import (
                    same_worker_command_records,
                    validate_worker_renewal_candidate,
                )

                genesis = load_genesis(self)
                actor_keys = load_effective_actor_keys(self, genesis)
                candidate_records = validate_worker_renewal_candidate(
                    claim_candidate,
                    genesis=genesis,
                    actor_keys=actor_keys,
                    registration=registration,
                    claim_id=claim_id,
                    claim_epoch=epoch,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
                if runtime_records and not same_worker_command_records(
                    tuple(runtime_records), candidate_records
                ):
                    self._conn.rollback()
                    return None
            elif runtime_records:
                self._conn.rollback()
                raise ValueError(
                    "renewal runtime_records require their signed claim_candidate"
                )

            if any(
                self.get_runtime_record(record.id) is not None
                for record in candidate_records
            ):
                self._conn.rollback()
                return None
            cursor = self._conn.execute(
                "UPDATE worker_claims SET lease_until = ?, updated_at = ? "
                "WHERE claim_id = ? AND epoch = ? AND worker_id = ? "
                "AND status = 'active' AND lease_until >= ?",
                (deadline, now_iso(), claim_id, epoch, worker_id, now.isoformat()),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return None
            for record in candidate_records:
                self._insert_runtime_record(record)
            renewal_request = next(
                (
                    record
                    for record in candidate_records
                    if record.record_type == "worker.claim.renew.requested"
                ),
                None,
            )
            causal_parents = [self._claim_granted_record_id(claim_id)]
            if renewal_request is not None:
                causal_parents.append(renewal_request.id)
            self._insert_runtime_record(
                create_runtime_record(
                    "claim.renewed",
                    {
                        "claim_id": claim_id,
                        "epoch": epoch,
                        "lease_until": deadline,
                        "worker_id": worker_id,
                    },
                    causal_parents=causal_parents,
                )
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return None
        except sqlite3.OperationalError as exc:
            self._conn.rollback()
            if _is_sqlite_contention(exc):
                return None
            raise
        except Exception:
            self._conn.rollback()
            raise
        return self.get_claim(row["contract_id"])

    def mark_claim_submitted(self, claim_id: str) -> None:
        with self._conn:
            row = self._conn.execute(
                "SELECT contract_id, worker_id, epoch, package_id "
                "FROM worker_claims WHERE claim_id = ? AND status = 'active'",
                (claim_id,),
            ).fetchone()
            if row is None:
                return
            self._conn.execute(
                "UPDATE worker_claims SET status = 'submitted', updated_at = ? "
                "WHERE claim_id = ? AND status = 'active'",
                (now_iso(), claim_id),
            )
            self._insert_runtime_record(
                create_runtime_record(
                    "claim.submitted",
                    {
                        "claim_id": claim_id,
                        "contract_id": row["contract_id"],
                        "epoch": int(row["epoch"]),
                        "package_id": row["package_id"],
                        "worker_id": row["worker_id"],
                    },
                    causal_parents=[self._claim_granted_record_id(claim_id)],
                )
            )

    def _claim_granted_record_id(self, claim_id: str) -> str:
        """Return the canonical causal root for one claim lifecycle."""
        rows = self._conn.execute(
            "SELECT record_id, payload_json FROM runtime_records "
            "WHERE record_type = 'claim.granted' ORDER BY revision DESC"
        ).fetchall()
        for row in rows:
            if json.loads(row["payload_json"]).get("claim_id") == claim_id:
                return str(row["record_id"])
        raise RuntimeError(f"claim {claim_id!r} has no immutable grant fact")

    def rebuild_claim_projection(self) -> None:
        """Rebuild the transactional claim head from immutable claim facts."""
        records = [
            record
            for _, record in self.scan_runtime_records()
            if record.record_type.startswith("claim.")
        ]
        with self._conn:
            self._conn.execute("DELETE FROM worker_claims")
            for record in records:
                payload = record.payload
                if record.record_type == "claim.granted":
                    self._conn.execute(
                        """INSERT INTO worker_claims (
                            claim_id, contract_id, worker_id, lease_until, status,
                            package_id, epoch, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                        (
                            payload["claim_id"],
                            payload["contract_id"],
                            payload["worker_id"],
                            payload["lease_until"],
                            payload["package_id"],
                            int(payload["epoch"]),
                            record.recorded_at,
                            record.recorded_at,
                        ),
                    )
                elif record.record_type == "claim.renewed":
                    self._conn.execute(
                        "UPDATE worker_claims SET lease_until = ?, updated_at = ? "
                        "WHERE claim_id = ? AND epoch = ?",
                        (
                            payload["lease_until"],
                            record.recorded_at,
                            payload["claim_id"],
                            int(payload["epoch"]),
                        ),
                    )
                elif record.record_type in {
                    "claim.expired",
                    "claim.released",
                    "claim.submitted",
                }:
                    status = record.record_type.removeprefix("claim.")
                    self._conn.execute(
                        "UPDATE worker_claims SET status = ?, updated_at = ? "
                        "WHERE claim_id = ? AND epoch = ?",
                        (
                            status,
                            record.recorded_at,
                            payload["claim_id"],
                            int(payload["epoch"]),
                        ),
                    )

    def get_idempotency(self, contract_id: str, idempotency_key: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT result_json FROM idempotency_records "
            "WHERE contract_id = ? AND idempotency_key = ?",
            (contract_id, idempotency_key),
        )
        row = cur.fetchone()
        return json.loads(row["result_json"]) if row else None

    def has_any_idempotency(self, contract_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM idempotency_records WHERE contract_id = ? LIMIT 1",
            (contract_id,),
        )
        return cur.fetchone() is not None

    def set_idempotency(
        self, contract_id: str, idempotency_key: str, result: dict
    ) -> None:
        existing = self.get_idempotency(contract_id, idempotency_key)
        if existing is not None:
            if existing == result:
                return
            raise ImmutableRecordConflict(
                "idempotency record", f"{contract_id}:{idempotency_key}"
            )
        try:
            self._conn.execute(
                "INSERT INTO idempotency_records "
                "(contract_id, idempotency_key, result_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    contract_id,
                    idempotency_key,
                    json.dumps(result, sort_keys=True),
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            if self.get_idempotency(contract_id, idempotency_key) == result:
                return
            raise ImmutableRecordConflict(
                "idempotency record", f"{contract_id}:{idempotency_key}"
            ) from None

    def _lock_worker_key_binding(self, worker_id: str, key_id: str) -> None:
        """Acquire writer arbitration and verify the current routing identity."""
        # A SELECT does not start SQLite's write transaction. This no-op write
        # prevents another replica from rebinding the worker before commitment.
        self._conn.execute(
            "UPDATE worker_registrations SET updated_at = updated_at "
            "WHERE worker_id = ?",
            (worker_id,),
        )
        registration = self._conn.execute(
            "SELECT actor_id, key_id, enabled FROM worker_registrations "
            "WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()
        if (
            registration is None
            or not bool(registration["enabled"])
            or registration["actor_id"] != worker_id
            or registration["key_id"] != key_id
        ):
            raise WorkerBindingConflict(
                "worker actor-key binding changed during submit"
            )

    def commit_worker_invocation_failure(
        self,
        *,
        trace_entry: TraceEntry,
        runtime_records: tuple[RuntimeRecord, ...],
        claim_id: str,
        worker_id: str,
        package_id: str,
        claim_epoch: int,
    ) -> bool:
        """Atomically record provider failure, terminal fact, and fenced release."""
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            recorded_at = now_iso()
            cursor = self._conn.execute(
                "UPDATE worker_claims SET status = 'released', updated_at = ? "
                "WHERE claim_id = ? AND status = 'active' AND worker_id = ? "
                "AND package_id = ? AND epoch = ?",
                (
                    recorded_at,
                    claim_id,
                    worker_id,
                    package_id,
                    claim_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError(
                    "active worker claim predicate failed during invocation failure"
                )
            claim_row = self._conn.execute(
                "SELECT contract_id, worker_id, epoch, package_id "
                "FROM worker_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            failure_parents = [
                record.id
                for record in runtime_records
                if record.record_type == "worker.invocation_failed"
            ]
            released = create_runtime_record(
                "claim.released",
                {
                    "claim_id": claim_id,
                    "contract_id": claim_row["contract_id"],
                    "epoch": int(claim_row["epoch"]),
                    "package_id": claim_row["package_id"],
                    "worker_id": claim_row["worker_id"],
                },
                causal_parents=[self._claim_granted_record_id(claim_id)]
                + failure_parents,
                recorded_at=recorded_at,
            )
            pending = runtime_records + (released,)
            derived = self._commit_time_ingress_consequences((), (), (), pending)
            pending += derived[1]
            self._validate_allowance_records(pending)
            self._insert_trace_entry(trace_entry)
            for record in pending:
                self._insert_runtime_record(record)
        return True

    def commit_claim_expiration(
        self,
        *,
        trace_entry: TraceEntry,
        runtime_records: tuple[RuntimeRecord, ...],
        claim_id: str,
        claim_epoch: int,
        expected_lease_until: str,
        observed_at: str,
    ) -> bool:
        """Atomically turn an expired lease into durable failure facts."""
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                "UPDATE worker_claims SET status = 'expired', updated_at = ? "
                "WHERE claim_id = ? AND epoch = ? AND status = 'active' "
                "AND lease_until = ?",
                (observed_at, claim_id, claim_epoch, expected_lease_until),
            )
            if cursor.rowcount != 1:
                return False
            if not any(
                record.record_type == "claim.expired" for record in runtime_records
            ):
                raise ValueError("claim expiration commit requires claim.expired fact")
            derived = self._commit_time_ingress_consequences(
                (), (), (), runtime_records
            )
            pending = runtime_records + derived[1]
            self._validate_allowance_records(pending)
            self._insert_trace_entry(trace_entry)
            for record in runtime_records:
                self._insert_runtime_record(record)
            for record in derived[1]:
                self._insert_runtime_record(record)
        return True

    def commit_ingress_batch(
        self,
        accepted_assets: list[Asset],
        trace_entries: list[TraceEntry],
        *,
        contract: Contract | None = None,
        contracts: tuple[Contract, ...] = (),
        reducer_callback: Callable[[], list[TraceEntry]] | None = None,
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
    ) -> tuple[tuple[TraceEntry, ...], tuple[RuntimeRecord, ...]]:
        with self._conn:
            # Conditional allowance/key/claim checks must observe the same
            # single-writer snapshot as their inserts, even for batches that
            # contain no Contract or Asset row to acquire the lock first.
            self._conn.execute("BEGIN IMMEDIATE")
            if self._candidate_already_committed(candidate_id):
                return (), ()
            if claim_binding is not None and self._validate_claim_binding(
                claim_binding,
                candidate_actor_id,
                candidate_key_id,
                candidate_id,
            ):
                return (), ()
            if validate_snapshot is not None:
                validate_snapshot()
            has_assignment_fact = any(
                record.record_type == "claim.assignment.changed"
                for record in runtime_records
            )
            if has_assignment_fact and validate_snapshot is None:
                from aigineering.core.claim_assignments import AssignmentConflict

                raise AssignmentConflict(
                    "assignment facts require the registered Candidate projection validator"
                )
            if (
                has_assignment_fact
                or assignment_intents
                or successor_assignment_witnesses
            ):
                from aigineering.core.claim_assignments import (
                    AssignmentConflict,
                    assignment_context_from_projection,
                    validate_assignment_batch,
                )
                from aigineering.core.projection_context import (
                    load_effect_projection_context,
                )
                from aigineering.core.actor_facts import load_effective_actor_keys
                from aigineering.core.domain import load_genesis

                if validate_snapshot is None or candidate_proposal is None:
                    raise AssignmentConflict(
                        "assignment facts require their signed Candidate projection"
                    )
                genesis = load_genesis(self)
                actor_keys = load_effective_actor_keys(self, genesis)
                assignment_context = assignment_context_from_projection(
                    load_effect_projection_context(self, actor_keys=actor_keys)
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
                    raise AssignmentConflict("assignment Candidate receipt is missing")
                validate_assignment_batch(
                    assignment_context,
                    runtime_records,
                    contracts,
                    candidate=candidate_proposal,
                    receipt_id=receipt.id,
                    effect_fact_owners=tuple(effect_fact_owners),
                    assignment_intents=tuple(assignment_intents),
                    successor_assignment_witnesses=tuple(
                        successor_assignment_witnesses
                    ),
                )
            has_hold_transition = any(
                record.record_type in {"claim.hold.placed", "claim.hold.released"}
                for record in runtime_records
            )
            has_constraint_transition = any(
                record.record_type
                in {"claim.constraint.placed", "claim.constraint.released"}
                for record in runtime_records
            )
            existing_records = (
                tuple(record for _, record in self.scan_runtime_records())
                if has_hold_transition or has_constraint_transition
                else ()
            )
            new_placements = (
                validate_claim_hold_batch(
                    existing_records,
                    runtime_records,
                    candidate_id=candidate_id,
                    actor_id=candidate_actor_id,
                    key_id=candidate_key_id,
                )
                if has_hold_transition
                else ()
            )
            for placement in new_placements:
                target = str(placement.payload["contract_id"])
                declarations_for_holds = (
                    (contract,) if contract is not None else ()
                ) + tuple(contracts)
                declared_ids = {item.id for item in declarations_for_holds}
                if target not in declared_ids and self.get_contract(target) is None:
                    raise ClaimHoldConflict("claim hold references an unknown Contract")
                has_terminal_fact = any(
                    record.record_type == "lifecycle.terminal"
                    and record.payload.get("contract_id") == target
                    for record in existing_records + runtime_records
                )
                if has_terminal_fact:
                    raise ClaimHoldConflict(
                        "cannot place a hold on a terminal Contract"
                    )
                claim_row = self._conn.execute(
                    "SELECT 1 FROM worker_claims WHERE contract_id = ? LIMIT 1",
                    (target,),
                ).fetchone()
                has_claim_fact = any(
                    record.record_type == "claim.granted"
                    and record.payload.get("contract_id") == target
                    for record in existing_records
                )
                if claim_row is not None or has_claim_fact:
                    raise ClaimHoldConflict(
                        "cannot place a hold on a Contract with claim history"
                    )
            if has_constraint_transition:
                from aigineering.core.claim_constraints import (
                    validate_claim_constraint_batch,
                )
                from aigineering.core.output_satisfaction import all_outputs_satisfied

                declared_contracts = (
                    (contract,) if contract is not None else ()
                ) + tuple(contracts)
                all_contracts = tuple(self.get_all_contracts()) + declared_contracts
                known_contract_ids = frozenset(item.id for item in all_contracts)
                claimed_ids = {
                    str(record.payload.get("contract_id", ""))
                    for record in existing_records
                    if record.record_type == "claim.granted"
                }
                claimed_ids.update(
                    str(row[0])
                    for row in self._conn.execute(
                        "SELECT DISTINCT contract_id FROM worker_claims"
                    )
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
                    claimed_contract_ids=frozenset(claimed_ids),
                    terminal_contract_ids=terminal_ids,
                    output_satisfied_contract_ids=output_satisfied_ids,
                )
            declarations = ((contract,) if contract is not None else ()) + tuple(
                contracts
            )
            self._validate_output_qualification_records(runtime_records)
            for declaration in declarations:
                self._insert_contract(declaration)
            self._validate_allowance_records(
                runtime_records,
                candidate_id=candidate_id,
                candidate_proposal=candidate_proposal,
                effect_fact_owners=tuple(effect_fact_owners),
            )
            self._insert_ingress_assets(accepted_assets)
            check_crash_point("after_asset_before_trace")
            derived_traces, derived_records = self._commit_time_ingress_consequences(
                accepted_assets,
                declarations,
                trace_entries,
                runtime_records,
            )
            trace_entries = list(trace_entries) + list(derived_traces)
            runtime_records = runtime_records + derived_records
            self._validate_allowance_records(
                runtime_records,
                candidate_id=candidate_id,
                candidate_proposal=candidate_proposal,
                effect_fact_owners=tuple(effect_fact_owners),
            )
            if reducer_callback is not None:
                reducer_traces: list[TraceEntry] = reducer_callback()
                trace_entries = list(trace_entries) + reducer_traces
            for entry in trace_entries:
                self._insert_trace_entry(entry)
            check_crash_point("after_trace_before_runtime_records")
            for record in runtime_records:
                if record.record_type in {
                    "claim.hold.placed",
                    "claim.hold.released",
                }:
                    self._insert_runtime_record(record, allow_claim_hold_fact=True)
                elif record.record_type in {
                    "claim.constraint.placed",
                    "claim.constraint.released",
                }:
                    self._insert_runtime_record(
                        record, allow_claim_constraint_fact=True
                    )
                elif record.record_type == "claim.assignment.changed":
                    self._insert_runtime_record(
                        record, allow_claim_assignment_fact=True
                    )
                else:
                    self._insert_runtime_record(record)
            if any(
                record.record_type == "lifecycle.terminal" for record in runtime_records
            ):
                check_crash_point("after_terminal_before_claim_release")
            self._release_claims_for_terminal_records(
                runtime_records,
                submitting_claim_id=(
                    claim_binding.claim_id if claim_binding is not None else ""
                ),
            )
            if claim_binding is not None:
                self._finalize_claim_submission(
                    claim_binding,
                    candidate_actor_id,
                    candidate_id,
                    runtime_records,
                )
            return derived_traces, derived_records

    def _commit_time_ingress_consequences(
        self,
        accepted_assets: list[Asset],
        declarations: tuple[Contract, ...],
        trace_entries: list[TraceEntry],
        runtime_records: tuple[RuntimeRecord, ...],
    ) -> tuple[tuple[TraceEntry, ...], tuple[RuntimeRecord, ...]]:
        """Close races between pure projection and serialized commitment.

        Candidate reduction normally derives these facts before the write
        transaction.  A concurrent Candidate may commit another output before
        this transaction acquires SQLite's writer lock.  Re-running the same
        pure reducer against the transaction-visible snapshot materializes
        only consequences that the earlier snapshot could not observe.
        """
        from aigineering.core.causal_allowance import materialize_terminal_allowance

        if accepted_assets:
            from aigineering.core.fact_materialization import (
                reduce_asset_facts,
                trace_records,
            )

            projected_traces, projected_records = reduce_asset_facts(
                self,
                self,
                accepted_assets,
                pending_contracts=declarations,
            )
        else:
            projected_traces, projected_records = (), ()
        known_trace_ids = {entry.id for entry in trace_entries}
        known_record_ids = {record.id for record in runtime_records}
        new_traces = tuple(
            entry for entry in projected_traces if entry.id not in known_trace_ids
        )
        projected_trace_records = trace_records(new_traces) if accepted_assets else ()
        candidates = tuple(projected_records) + projected_trace_records
        new_records = tuple(
            record for record in candidates if record.id not in known_record_ids
        )
        allowance_records = materialize_terminal_allowance(
            self,
            declarations,
            runtime_records + new_records,
        )
        known_record_ids.update(record.id for record in new_records)
        new_records += tuple(
            record for record in allowance_records if record.id not in known_record_ids
        )
        return new_traces, new_records

    def _candidate_already_committed(self, candidate_id: str) -> bool:
        return bool(
            candidate_id
            and self._conn.execute(
                "SELECT 1 FROM runtime_records WHERE record_type = "
                "'candidate.committed' AND "
                "json_extract(payload_json, '$.candidate_id') = ? LIMIT 1",
                (candidate_id,),
            ).fetchone()
        )

    def _validate_claim_binding(
        self, claim_binding, actor_id: str, key_id: str, candidate_id: str
    ) -> bool:
        """Validate the claim fence; return True for an exact closed replay."""
        if actor_id == "" or key_id == "":
            raise ValueError("claim-bound commitment requires actor key binding")
        self._lock_worker_key_binding(actor_id, key_id)
        existing = self._conn.execute(
            "SELECT contract_id, worker_id, epoch, package_id, status, lease_until "
            "FROM worker_claims WHERE claim_id = ?",
            (claim_binding.claim_id,),
        ).fetchone()
        if existing is None:
            raise ClaimBindingConflict("claim-bound Candidate has unknown claim")
        if existing["status"] == "submitted" and any(
            record.payload.get("claim_id") == claim_binding.claim_id
            and record.payload.get("candidate_id") == candidate_id
            for _, record in self.scan_runtime_records(record_type="claim.submitted")
        ):
            return True
        terminal = self._conn.execute(
            "SELECT 1 FROM runtime_records WHERE record_type = "
            "'lifecycle.terminal' AND "
            "json_extract(payload_json, '$.contract_id') = ? LIMIT 1",
            (claim_binding.contract_id,),
        ).fetchone()
        if terminal is not None and existing["status"] != "submitted":
            raise ClaimBindingConflict(
                "claim-bound Candidate references terminal Contract"
            )
        if (
            existing["status"] != "active"
            or existing["contract_id"] != claim_binding.contract_id
            or existing["worker_id"] != actor_id
            or int(existing["epoch"]) != claim_binding.claim_epoch
            or existing["package_id"] != claim_binding.package_id
            or existing["lease_until"] < now_iso()
        ):
            raise ClaimBindingConflict(
                "active worker claim predicate failed during Candidate commit"
            )
        return False

    def _validate_output_qualification_records(
        self, runtime_records: tuple[RuntimeRecord, ...]
    ) -> None:
        if not any(
            record.record_type == "output.qualified" for record in runtime_records
        ):
            return
        from aigineering.core.acceptance import validate_output_qualification_commit

        validate_output_qualification_commit(
            tuple(record for _, record in self.scan_runtime_records()),
            runtime_records,
        )

    def _narrow_allowance_commit_inputs(
        self,
        runtime_records: tuple[RuntimeRecord, ...],
        candidate_id: str,
        *,
        candidate_proposal=None,
        effect_fact_owners=(),
    ):
        """Select exact inputs for a canonical declaration-only allowance batch.

        Any uncertainty returns None so the existing full-history validator
        retains its original ordering and rejection behavior.
        """
        from aigineering.core.allowance_publication_index import (
            DeclarationPairsRequest,
            AllowanceBalanceInput,
            build_canonical_allowance_declaration_batch,
            publication_closure_for_funding_batch,
            publication_closure_for_heads,
            publication_declaration_pairs,
            publication_heads_for_declarations,
            publication_heads_for_funding_facts,
        )

        if self._allowance_checkpoint_mode == "reference":
            return None
        batch = build_canonical_allowance_declaration_batch(
            runtime_records,
            candidate_id,
            candidate_proposal=candidate_proposal,
            effect_fact_owners=effect_fact_owners,
        )
        if batch is None:
            return None
        incoming = batch.incoming_contracts
        declarations = batch.declarations
        pending_facts = batch.funding_facts

        pending_ids = tuple(dict.fromkeys(record.id for record in runtime_records))
        pending_chunk_size = self._publication_query_chunk_size(1)
        for start in range(0, len(pending_ids), pending_chunk_size):
            chunk = pending_ids[start : start + pending_chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            if self._conn.execute(
                "SELECT 1 FROM runtime_records WHERE record_id IN ("
                + placeholders
                + ") LIMIT 1",
                chunk,
            ).fetchone():
                return None
        existing_candidate = self._conn.execute(
            "SELECT 1 FROM runtime_records WHERE "
            "(record_type='candidate.received' AND "
            "json_extract(payload_json,'$.candidate_id')=?) OR "
            "(record_type='candidate.committed' AND "
            "json_extract(payload_json,'$.candidate_id')=?) LIMIT 1",
            (candidate_id, candidate_id),
        ).fetchone()
        if existing_candidate is not None:
            return None

        def records_for_ids(ids: tuple[str, ...]) -> tuple[RuntimeRecord, ...]:
            selected: dict[str, tuple[int, RuntimeRecord]] = {}
            if not ids:
                return ()
            chunk_size = self._publication_query_chunk_size(1)
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start : start + chunk_size]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                for row in self._conn.execute(
                    "SELECT * FROM runtime_records WHERE record_id IN ("
                    + placeholders
                    + ")",
                    chunk,
                ).fetchall():
                    record = self._row_to_runtime_record(row)
                    selected[record.id] = (int(row["revision"]), record)
            return tuple(
                record for _, record in sorted(selected.values(), key=lambda x: x[0])
            )

        source_ids = tuple(
            dict.fromkeys(
                contract.parent_id for contract in incoming if contract.parent_id
            )
        )
        contract_ids = tuple(
            dict.fromkeys((*[contract.id for contract in incoming], *source_ids))
        )
        existing_contracts = tuple(
            contract
            for contract_id in contract_ids
            if (contract := self.get_contract(contract_id)) is not None
        )
        existing_contract_ids = {contract.id for contract in existing_contracts}
        contracts = existing_contracts + tuple(
            contract
            for contract in incoming
            if contract.id not in existing_contract_ids
        )
        if len({contract.id for contract in contracts}) != len(contract_ids):
            return None

        if not pending_facts:
            # Declaration batches without allowance changes do not use this path.
            return None
        first = self.read_allowance_publication_selection(
            publication_heads_for_funding_facts(batch)
        )
        if first is None:
            return None
        if first.records:
            # New fact IDs cannot already have an accepted owner. Preserve the
            # reference validator's replay/ambiguity ordering on that history.
            return None
        pairs = publication_declaration_pairs(batch)
        if pairs is None:
            return None
        declaration_fact_ids: list[str] = []
        first_closure_request = publication_closure_for_funding_batch(batch)
        if first_closure_request is None:
            return None
        first_closure = self.read_allowance_publication_selection(first_closure_request)
        if first_closure is None:
            return None

        # Exact declarations are selected for every candidate/target pair; the
        # pending declaration remains in the caller-owned overlay.
        declaration_selection = self.read_allowance_publication_selection(
            DeclarationPairsRequest(pairs)
        )
        if declaration_selection is None:
            return None
        declaration_fact_ids.extend(
            record.id for record in declaration_selection.records
        )
        pair_set = set(pairs)
        declaration_fact_ids.extend(
            record.id
            for record in declarations
            if (
                record.payload.get("candidate_id"),
                record.payload.get("contract", {}).get("id"),
            )
            in pair_set
        )

        second_member_ids = tuple(dict.fromkeys(declaration_fact_ids))
        second = self.read_allowance_publication_selection(
            publication_heads_for_declarations(second_member_ids)
        )
        if second is None:
            return None
        second_closure = self.read_allowance_publication_selection(
            publication_closure_for_heads(second_member_ids, second.records)
        )
        if second_closure is None:
            return None

        selected: dict[str, RuntimeRecord] = {}
        for group in (
            first_closure.records,
            declaration_selection.records,
            second_closure.records,
        ):
            selected.update((record.id, record) for record in group)
        selected.update(
            (record.id, record)
            for record in records_for_ids(
                tuple(
                    record.id
                    for record in runtime_records
                    if record.record_type in {"allowance.granted", "allowance.reserved"}
                )
            )
        )
        target_ids = tuple(dict.fromkeys(contract.id for contract in incoming))
        target_chunk_size = self._publication_query_chunk_size(1) if target_ids else 1
        for start in range(0, len(target_ids), target_chunk_size):
            chunk = target_ids[start : start + target_chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                "SELECT * FROM runtime_records WHERE record_type='allowance.granted' "
                "AND json_extract(payload_json,'$.contract_id') IN ("
                + placeholders
                + ")",
                chunk,
            ).fetchall()
            for row in rows:
                record = self._row_to_runtime_record(row)
                selected[record.id] = record
        source_chunk_size = self._publication_query_chunk_size(1) if source_ids else 1
        for start in range(0, len(source_ids), source_chunk_size):
            chunk = source_ids[start : start + source_chunk_size]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                "SELECT * FROM runtime_records WHERE record_type='lifecycle.terminal' "
                "AND json_extract(payload_json,'$.contract_id') IN ("
                + placeholders
                + ")",
                chunk,
            ).fetchall()
            for row in rows:
                record = self._row_to_runtime_record(row)
                selected[record.id] = record

        balances = []
        for source_id in source_ids:
            checkpoint = self.read_raw_allowance_checkpoint(source_id)
            if checkpoint.status != "valid" or checkpoint.totals is None:
                return None
            balances.append(AllowanceBalanceInput(source_id, checkpoint.totals))
        return contracts, tuple(selected.values()), tuple(balances)

    def _validate_allowance_records(
        self,
        runtime_records: tuple[RuntimeRecord, ...],
        *,
        candidate_id: str | None = None,
        candidate_proposal=None,
        effect_fact_owners=(),
    ) -> None:
        if not any(
            record.record_type
            in {
                "allowance.granted",
                "allowance.reserved",
                "allowance.returned",
                "allowance.extinguished",
            }
            for record in runtime_records
        ):
            return
        from aigineering.core.causal_allowance import validate_allowance_commit

        narrow = (
            self._narrow_allowance_commit_inputs(
                runtime_records,
                candidate_id,
                candidate_proposal=candidate_proposal,
                effect_fact_owners=effect_fact_owners,
            )
            if candidate_id is not None
            else None
        )
        if narrow is None:
            existing_records = tuple(
                record for _, record in self.scan_runtime_records()
            )
            contracts = tuple(self.get_all_contracts())
            balances = None
        else:
            contracts, existing_records, balances = narrow
        validate_allowance_commit(
            contracts,
            existing_records,
            runtime_records,
            candidate_id=candidate_id,
            balance_inputs=balances,
        )

    def _insert_ingress_assets(self, accepted_assets: list[Asset]) -> None:
        for asset in accepted_assets:
            if not asset.signed_by or not verify_asset_seal(asset):
                raise ValueError(
                    f"Asset '{asset.id}' rejected — "
                    "missing or invalid canonical seal "
                    f"(signed_by={asset.signed_by!r})"
                )
            self._insert_asset(asset)

    def _finalize_claim_submission(
        self,
        claim_binding,
        actor_id: str,
        candidate_id: str,
        runtime_records: tuple[RuntimeRecord, ...],
    ) -> None:
        committed_at = now_iso()
        cursor = self._conn.execute(
            "UPDATE worker_claims SET status = 'submitted', updated_at = ? "
            "WHERE claim_id = ? AND status = 'active' AND worker_id = ? "
            "AND contract_id = ? AND package_id = ? AND epoch = ? "
            "AND lease_until >= ?",
            (
                committed_at,
                claim_binding.claim_id,
                actor_id,
                claim_binding.contract_id,
                claim_binding.package_id,
                claim_binding.claim_epoch,
                committed_at,
            ),
        )
        if cursor.rowcount != 1:
            raise ClaimBindingConflict("claim changed during Candidate commitment")
        decision_parents = [
            record.id
            for record in runtime_records
            if record.record_type == "candidate.committed"
        ]
        self._insert_runtime_record(
            create_runtime_record(
                "claim.submitted",
                {
                    "candidate_id": candidate_id,
                    "claim_id": claim_binding.claim_id,
                    "contract_id": claim_binding.contract_id,
                    "epoch": claim_binding.claim_epoch,
                    "package_id": claim_binding.package_id,
                    "worker_id": actor_id,
                },
                causal_parents=[self._claim_granted_record_id(claim_binding.claim_id)]
                + decision_parents,
            )
        )

    # ------------------------------------------------------------------
    # Replacement claim persistence
    # ------------------------------------------------------------------

    def add_replacement_claim(self, claim) -> None:
        with self._conn:
            self._insert_replacement_claim(claim)

    def _insert_replacement_claim(self, claim) -> None:
        row = self._conn.execute(
            "SELECT * FROM claims WHERE id = ?", (claim.id,)
        ).fetchone()
        if row is not None:
            existing = self._row_to_replacement_claim(row)
            if existing == claim:
                return
            raise ImmutableRecordConflict("replacement claim", claim.id)
        try:
            self._conn.execute(
                """INSERT INTO claims (
                    id, source_asset_id, replacement_asset_id,
                    definition_hash, claim_type, signed_by,
                    provenance_seal, lineage_id, derivation_version, range_spec
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    claim.id,
                    claim.source_asset_id,
                    claim.replacement_asset_id,
                    claim.definition_hash,
                    claim.claim_type,
                    claim.signed_by,
                    claim.provenance_seal,
                    claim.lineage_id,
                    claim.derivation_version,
                    claim.range_spec,
                ),
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                "SELECT * FROM claims WHERE id = ?", (claim.id,)
            ).fetchone()
            if row is not None and self._row_to_replacement_claim(row) == claim:
                return
            raise ImmutableRecordConflict("replacement claim", claim.id) from None

    def commit_replacement_claim(self, claim, trace_entry: TraceEntry) -> None:
        """Persist a replacement claim and its audit trace atomically."""
        with self._conn:
            self._insert_replacement_claim(claim)
            check_crash_point("after_replacement_claim_before_trace")
            self._insert_trace_entry(trace_entry)
            claim_record = create_runtime_record(
                "replacement.claimed",
                {"claim": replacement_claim_payload(claim)},
            )
            self._insert_runtime_record(claim_record)
            self._insert_runtime_record(
                create_runtime_record(
                    "trace.recorded",
                    {"trace": trace_entry_to_dict(trace_entry)},
                    causal_parents=[claim_record.id],
                )
            )

    def get_claims_by_definition(self, definition_hash: str) -> list:
        rows = self._conn.execute(
            "SELECT * FROM claims WHERE definition_hash = ?",
            (definition_hash,),
        ).fetchall()
        return [self._row_to_replacement_claim(r) for r in rows]

    def get_claims_for_asset(self, asset_id: str) -> list:
        rows = self._conn.execute(
            "SELECT * FROM claims WHERE source_asset_id = ?",
            (asset_id,),
        ).fetchall()
        return [self._row_to_replacement_claim(r) for r in rows]

    def get_claims_for_replacement_asset(self, asset_id: str) -> list:
        rows = self._conn.execute(
            "SELECT * FROM claims WHERE replacement_asset_id = ?",
            (asset_id,),
        ).fetchall()
        return [self._row_to_replacement_claim(r) for r in rows]

    _row_to_replacement_claim = staticmethod(replacement_claim_from_row)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
