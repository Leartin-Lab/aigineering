# Signed task holds: development evidence

Date: 2026-10-04
Source version: 0.5.13.dev0; no release tag or stable qualification
Base revision: `01be3f4e585acd7975284a38e88dbdcbb44724c0`, with local development edits

## Snapshot and behavior

The verified working-tree source snapshot has 380 files and SHA-256
`091ac4ac692fd1b786dd19e0ab2dce57799c1d3a9de830b6deddf74bd8bf2d4a`.
Its scope and hashing method match [report 074](074-signed-task-comments-2026-10-04.md):
Python source, tests, scripts and examples; example TOML and project/Compose
configuration; public conformance JSON and script MJS files. Documentation is
checked separately for freshness and governance.

`task block/unblock/holds` provides authenticated, attributed holds on unclaimed
tasks. Any open hold prevents ordinary pull and direct Store claim acquisition.
Releasing one exact hold preserves others. Both claim and hold transactions
arbitrate under SQLite's writer lock; a stale task view is insufficient.
Competing releases accept exactly one new Candidate. Same-input keyed replay
returns the original record after release, claim or terminal changes.

Collaboration commands also provide readable default output and optional JSON.
The displayed full IDs identify comments, hold placements and exact releases.
No authoritative mutable card or hold table was introduced.

## Verification

- Full serial suite: **1714 passed**, no skips, 6 dependency warnings, 66.34 s.
  Application Redis and the explicitly dedicated integration-test database used
  one isolated verification service; the normal Compose service was preserved.
- Ruff check and format check passed for 353 Python files, including the
  installed-artifact verification script.
- Independent focused runs passed **75 tests** for holds and **73 tests** for
  comments. Both included the same governance suite, so those numbers are not
  added together. Independent review verified the current implementation and
  strengthened claim-bound and replay assertions; author checks are separate.
- Four public hold-effect vectors passed Python consumption and independent
  Node checks of canonical signing bytes, Ed25519 signatures, Candidate IDs and
  runtime-record IDs. The comment vector remains valid.
- Forced two-connection tests cover block versus claim, release versus claim,
  competing releases and a claim winning after projection but before commitment.
  Permission, unsupported schema, authenticated fact provenance, multiple holds,
  historical revision cutoff, terminal cleanup and exact replay checks passed.
- Builds and `twine check` passed. Artifact checks confirm source-byte equality,
  public-document freshness and absence of private workspace material.
- A fresh wheel installation outside the source tree passed actual CLI/Redis
  publication of two holds, direct and pull claim rejection, partial then full
  release, exact replay, readable default output, JSON envelopes, event metadata,
  Redis reconstruction and SQLite reopen/rebuild. All 177 packaged Python files
  match source bytes. The surrounding Worker is explicitly a fixture, with no
  provider calls; the hold and relay operations use their actual implementations.

## Failures retained and corrected

The first focused collection failed on an incorrect test import. A subsequent
focused run had seven failures: an old registry assertion and Click callbacks
contaminated by main-entry protection. The registry test now checks its frozen
snapshot; registrations copy commands instead of sharing mutable callback state.

Independent review found a missing relay for transaction-race rejection.
SQLite had already recorded rejection before raising `CandidateCommitRejected`;
the application now synchronizes that outcome and reports pending synchronization
with acceptance false if Redis fails. Tests retain the rejected Candidate and
winning claim in both cases.

Review found that an initial claim-bound test lacked `worker.submit`, allowing
an earlier permission rejection to mask the intended assertion. It now supplies
base permissions and asserts the specific unsupported-effect rejection. A replay
test's self-comparison was replaced by exact count, identity and claim checks.

The first full run had **1 failed, 1713 passed**. A new insertion keyword broke
the existing actor-rotation crash-injection hook before its intended failure.
Ordinary facts now retain the original call convention; only validated hold
facts receive the explicit authorization flag. The original rollback test was
preserved, and the final full run passes. Earlier failure logs remain retained.

## Limits

These holds stop work before claim acquisition. Placement rejects any claim
history or terminal outcome. Active Worker pause, successor handoff, review and
change requests remain part of the continuing goal. Cleanup cannot reopen a
terminal task. A domain operator with `task.unblock` may release another actor's
exact hold; attribution remains that of the releasing actor.

This control slice has **zero provider calls**. Deterministic and installed
fixture evidence cannot requalify earlier live-provider output or establish
complete Kanban parity. No commit, push, tag or package publication occurred.

Decision: [ADR-029](../docs/adr/ADR-029-signed-claim-holds.md).
Change: [040](../changes/040-signed-task-holds.md).
Usage: [task operations](../docs/reference/task-board.md).
