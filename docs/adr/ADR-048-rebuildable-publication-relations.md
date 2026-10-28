# ADR-048: Rebuildable allowance publication relations

Status: Accepted for the SQLite allowance adapter.

## Context

Allowance validation needs complete committed-head and declaration relationships
to preserve competing-publication and receipt checks. Repeating whole-history
queries for each declaration publication keeps those proofs correct but makes
the selected SQLite path rescan unrelated records. A narrower query is safe
only when it supplies every candidate head and its complete proof closure to
the existing allowance owner.

## Decision

SQLite maintains a private, rebuildable relation index from committed-head
member IDs to every head containing each member. It also uses a non-unique
expression index for exact `(Candidate ID, Contract ID)` declaration pairs.
Neither index selects a winner: all matching heads and declarations remain
visible to the existing uniqueness and authorization checks.

The relation tables and their cursor/opaque-state row are derived data in the
same SQLite transaction as each newly inserted committed head. A missing,
damaged, opaque, or legacy-uncertain index selects the existing full proof path.
Explicit audit and rebuild operations verify or recreate the derived rows;
ordinary reads do not repair them. The indexes grant no authority and add no
new task or allowance state.

Core constructs immutable, typed requests and validates their safe shapes.
SQLite performs the corresponding reads in the current snapshot. The existing
allowance owner still validates the complete selected heads, receipts,
declarations, overlays, balances, and competing publications. Both existing
writer validations make fresh selections under the original transaction.

## Alternatives

Selecting only the current grant, source terminal, or one declaration would
hide competing heads and can change a rejection into acceptance. A process
cache would not cover other Store connections or restarts. Persisting a chosen
head would make derived data look authoritative. The rebuildable relation
index limits reads while leaving proof decisions with the existing owner.

## Consequences

Well-formed supported pure declaration batches can select their complete proof
inputs and source balances locally for projection and both writer-time
validations. Full fallback remains for opaque or legacy history, uncertain old-writer state,
missing or invalid cache state, unsupported effects, and replay cases that
cannot be safely narrowed. The index adds transactional maintenance and
audit/rebuild work. This decision does not optimize other runtime consumers or
establish that Asset, task-claim, or total sustained-workspace work is local;
those paths retain their own costs and require separate evidence.
