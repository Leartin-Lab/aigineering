# Task CLI and required Redis verification

Status: Local development verification passed; provider follow-up recorded separately
Date: 2026-10-03
Baseline: 01be3f4e585acd7975284a38e88dbdcbb44724c0
Source version: 0.5.13.dev0

## Scope and snapshot

[Change 038](../changes/038-required-redis-task-operations.md) implements shared
task views and publication, operational task commands, and required Redis
collaboration. [ADR-027](../docs/adr/ADR-027-required-redis-collaboration.md)
records the infrastructure decision; [task operations](../docs/reference/task-board.md)
documents commands and failure behavior. SQLite remains the signed fact and
claim authority. This evidence also rechecks the configured entry from change 037.

The tested working-tree snapshot covers 347 files: Python source, tests and
scripts, example `run.toml` presets, `pyproject.toml`, and `compose.yaml`.
SHA-256: `e8b337c71f8ac8285f6e692180cdc6388b6c59e23f4d1c09d4ead7c4047121a6`.
The fingerprint hashes compact sorted JSON mapping repository-relative paths to
file SHA-256 values. Public documentation is checked separately after this report.
Private evidence retains manifests, diagnostic logs and Store snapshots; raw
Stores and credentials are not release artifacts.

## Verification

| Check | Result |
| --- | --- |
| Ruff check and format check, including installed-smoke script | Passed; 340 files formatted |
| Serial full suite with both actual Redis services available | 1662 passed, no skips, 6 existing dependency warnings; 62.80 s |
| Standard isolated build with offline cached dependencies | Wheel and sdist built; wheel built from sdist |
| Fresh wheel installation outside the source tree | Passed; installed import location and version checked |
| Installed task list, board, stats, show, events and bounded JSONL watch | Passed against actual default Redis |
| Installed task result | Exact committed UTF-8 content exported; existing destination preserved |
| Installed Redis status, sync and domain-scoped rebuild | Passed; indexes and relay current |
| Closed-port failure and explicit offline task audit | Nonzero unavailable result; offline audit remained readable |
| Configured task resume, legacy Fleet entry and missing original input file | Passed in separate installed CLI processes |
| Installed ordinary task and configured task backup/reopen/reconstruction | Records unchanged; semantic digests equal |
| Redis concurrent relay and cross-domain isolation | Passed; atomic stream/watermark updates and no duplicate revisions |
| Fleet concurrency and target isolation | Passed; an earlier eligible unrelated root was not claimed |
| CLI/API post-commit relay fault injection | Committed facts retained; pending distinguished from rejection |
| Final public-document governance and root guide references | 48 checks passed; all 27 root guide references resolve |
| Artifact metadata, privacy and byte comparison | Twine passed; packaged Python and public-document bytes match source |

The repository Compose configuration was validated and started with Redis
7.4.11, AOF storage, a persistent volume and the default localhost port. A task
published through the normal signed CLI retained all six relay events after a
Redis container restart, without a repair operation. Before/after state digest:
`c1e25e32585044bcb2af08c00b116ad2ab9b6b7dc885246c921200f082128cae`.
A second isolated Redis service/database supported integration and concurrency
checks. Existing services were not restarted or changed.

Installed ordinary Store reconstruction digest:
`d5d695ae8e56cedca5257708ae29bbc3d477079f3c7a789b1eac3abec07f5a4b`.
Configured Store reconstruction digest:
`4380944c2617b50a08178535b810db6dd31b92ced860b40703f7dfda75604ab3`.
Both matched before and after reconstruction, with runtime records unchanged.

Task-board boundary tests cover exact independent qualification, sealed and
tombstoned output refusal, claim-aware cancellation, retry identity and frozen
context, missing tasks and truncation. HTTP boundary tests inject Redis failures
while exercising actual signed SQLite commitment; they are not claims of a live
HTTP deployment or provider acceptance.

## Corrected failures

Concurrent relay initially exposed a real race: separately reading metadata and
stream tail could mistake an advancing writer for corruption and trigger a
conflicting rebuild. Atomic Lua state reads and conditional clears fixed it;
the concurrent test then passed. Other reviewed fixes remove revision-only card
memoization, include typed allowance/qualification/claim-renewal references, and
prevent offline session inspection from creating directories.

An initial full run recorded 1631 passes and 10 failures while integration
fixtures still assumed optional Redis, empty-domain query fallback, old task
publication ownership, or reusable closed connections. The next run recorded
1656 passes and two failures: one stale design-text assertion and an
order-dependent test using shared Click commands already wrapped by another
module. Updating the assertion and testing copied commands with explicit
unwrapped callbacks fixed both. Production wrappers were retained. The final
snapshot passed the entire suite; earlier logs remain available privately.

## Limits and delivery state

Installed execution used explicitly selected deterministic fixture/mock Workers
for runtime and packaging coverage. Redis integration and restart used actual
infrastructure. No new provider call was made during this verification phase;
earlier DeepSeek results validate their historical snapshots only. Subsequent
authorization and a bounded real repository-report run are recorded in
[report 073](073-required-redis-live-report-2026-10-04.md), including retained
content-review defects. This is not a stable-release qualification or a claim
of improved model quality.

Manual block/unblock, comments, priority editing, multiple boards, large-board
performance qualification, and configurable quick-goal allowance remain future
work. The implemented commands provide the current basic operational workflow;
they do not establish full Hermes Kanban feature parity.

Delivery consists of local development edits, local Redis infrastructure and
built artifacts. No new commit, remote update, tag or package publication was made.
