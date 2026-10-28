# ADR-045: Signed task attachment links

## Status

Accepted for the bounded local attachment-link workflow.

## Context

The artifact-v1 ingress path signs a bounded attachment Asset, but it does not
associate that Asset with a particular task or provide one exact-byte export
command. Task comments are attributed text and cannot safely stand in for a
typed relation. Task context and Contract inputs are separate authority owners;
an attachment association must not widen either one.

## Decision

Add an application-owned task attachment service and a feature plugin with the
single `task.attachment.link` effect, `task.attachment.linked` fact, and
`task.attach` capability. A link Candidate contains exactly one effect,
the canonical task declaration, and an exact original attachment Asset
publication witness. It stores the relation as a signed append-only fact with
the task/Asset IDs, source receipt/head/member references, current actor/key,
Candidate ID, and signed idempotency key. The generic Candidate publisher and
SQLite writer revalidate the decision under the existing commitment boundary.

Historical publication inspection proves a unique immutable receipt, committed
head, ordered member list, causal parents, and canonical record IDs. The source
Asset must come from the supported sole `asset.propose` publication path. Task
declarations may come from the direct or multi-`contract.declare` path, or the
existing `task.handoff`, `task.revise`, and `task.review.request` compound
publishers when their exact typed relation facts and complete committed head
match the known shape. Unknown, ambiguous, foreign domain, malformed, or
unsupported history is unavailable. A historical
receipt/head is evidence that the runtime previously accepted that publication.
It does not contain enough of the original Candidate to verify the original
signature again. The current link Candidate is verified normally against
current authorization.

List validates all relation history for the selected task, uses a
scope-bound snapshot cursor, and emits only relation and Asset metadata. Export
requires an exact relation and verifies artifact-v1 byte length/hash before an
exclusive-create 0600 write. It never overwrites a destination or symlink. A
partial write can leave the file created by that failed attempt, which must be
removed explicitly before another export. Read/export operate on an existing
Store snapshot and do not repair or publish facts.

Redis remains required. A post-commit relay failure preserves the SQLite fact
and returns its exact accepted Candidate/fact IDs as `redis_sync_pending`; a
same-key replay after recovery returns the original link and synchronizes it.

## Alternatives considered

- Reuse task comments: rejected because free-form text is not a typed,
  replayable association and does not prove an Asset publication.
- Add Assets to Contract inputs or task context: rejected because that changes
  Worker disclosure and frozen task scope, which belong to separate owners.
- Add an attachment table or mutate the Asset: rejected because the existing
  signed Candidate/fact path already records attribution and replay without a
  second mutable source of truth.
- Copy bytes into the relation fact: rejected because it duplicates content
  and expands disclosure. The relation stores exact IDs and export reads the
  already committed Asset.

## Consequences

Links are append-only and do not change task lifecycle, inputs, Worker context,
or output qualification. Multiple explicit-key links may document the same
pair, but one idempotency key cannot be rebound to another pair. Missing or
unsupported old publication evidence fails closed. Export is an explicit local
copy; it does not grant remote access, provide multi-user ACLs, or promise OS
isolation. Historical publication proof does not re-verify an old Candidate
signature from incomplete receipt data.
