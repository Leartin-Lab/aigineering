# Signed task comments: development evidence

Date: 2026-10-04
Source version: 0.5.13.dev0; no release tag or stable qualification
Base revision: `01be3f4e585acd7975284a38e88dbdcbb44724c0`, with local development edits

## Snapshot and scope

The verified working-tree source snapshot has 374 files and SHA-256
`b83bf59c85588a318e12531cb5df2d6b0576108651af00e7c51a7f020b3754e8`.
It covers Python files under `src/aigineering`, `tests`, `scripts`, and `examples`,
example TOML files, `pyproject.toml`, `compose.yaml`, conformance JSON files and
script MJS files. Hash each file's bytes, sort the path-to-hash mapping, encode
that mapping as compact JSON with sorted keys, and hash the resulting bytes.
The scope extends earlier snapshots to include public vectors and their Node
consumer; their hashes are not directly comparable to the earlier scope.

This slice implements `task comment/comments`, signed attribution, same-task
replies and durable revision-based history. It does not implement human holds,
review requests, assignment/priority, editable dependencies, multiple boards,
scheduled dispatch, notifications or retention. The broader CLI goal continues.

## Verification

- Full serial suite: **1686 passed**, no skips, 6 dependency warnings, 63.60 s.
  Application Redis used a dedicated local service; its separate database 15
  was explicitly dedicated to integration tests that flush their test database.
- Ruff check passed; format check passed for 348 Python files, including the
  installed-artifact verification script.
- Independent focused review ran **71 tests**. A subsequent explicit regression
  verifies that a claim-bound Candidate cannot use an injected comment handler;
  the final full suite includes it.
- Public conformance vectors passed Python consumption and independent Node
  verification of signing bytes, Ed25519 signature, Candidate ID and record ID.
- Source distribution and wheel builds passed, as did `twine check`.
- Repository-external wheel installation exercised actual CLI processes against
  Redis: comment publication, reply, exact replay with original timestamp and
  revision, unchanged completed status, metadata-only event delivery, Redis
  rebuild, SQLite reconstruction and reopen. The Worker used for the surrounding
  task was explicitly a fixture, with **zero provider calls**.
- All 176 packaged Python files matched source bytes. Packaged public Markdown
  matched source. Public document governance, root guide references and artifact
  privacy checks passed after documentation closure.

## Failures retained and corrected

Initial governance rejected a core import of the collaboration plugin. The
application now explicitly injects a frozen additional-effect registry; the core
does not know the comment effect by default, and built-ins cannot be overridden.
A later coordinator-size check failed after adding the generic injection hook;
rejection persistence was delegated to a small helper with a compatible export.
The governance rules were preserved.

Independent review also found key validation occurring after identity provisioning
and uninitialized Genesis reported as a missing task. Both were corrected and
covered by regression tests. Redis failure after commitment retains facts and
reports pending synchronization; restoring service and replaying exact input
returns the existing durable comment.

## Limits

A key participates in Candidate identity. Same-key, same-input replay is
idempotent; changing text, target or reply creates another Candidate. There is
no key-only uniqueness promise. A signed comment is an attributed statement,
not output qualification or verified truth. This slice has no new live-provider
evidence and does not requalify earlier provider runs or claim stable release.
All changes remain local; no commit, push, tag or package publication occurred.

Decision: [ADR-028](../docs/adr/ADR-028-signed-task-comments.md).
Change: [039](../changes/039-signed-task-comments.md).
Usage: [task operations](../docs/reference/task-board.md).
