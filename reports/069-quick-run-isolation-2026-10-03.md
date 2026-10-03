# Quick-run output and claim isolation — 2026-10-03

Status: Locally validated maintenance changes; no remote release or live provider qualification.

## Scope and result

Repeated quick-goal runs previously declared the same `final_report` output
slot. Since ordinary output satisfaction is intentionally name-based across a
domain, an Asset from an earlier run could complete a new root before its
Worker executed. The quick-run adapter now assigns a UUID4-suffixed output name
per invocation. Consumers should locate the result through the root Contract's
`outputs` and the session's `produced_asset_ids`.

The quick-run claim loop selects the current root and its descendants by their
immutable `parent_id` links, then uses the existing exact-Contract claim path.
The tree is recomputed after each maintenance step, so newly published
descendants and recovery tasks with a parent in the tree can be claimed. This
does not narrow domain-wide maintenance or expired-claim processing, nor does
it change shared same-name output satisfaction or explicit task-pool runs.

A malformed result on the quick-run root produces a durable rejection and a
separate recovery successor; the root remains failed and terminal. Because the
root recovery successor is not a descendant of the root Contract, it requires
an explicit `aig run --task <contract_id>` call. A rejected child instead gets
a recovery successor with the same parent as that child, so it remains within
the root tree and can be claimed by the quick-run loop.

## Verification

- The focused CLI and quick-run regression set passed: 47 tests across
  `tests/test_quick_run_isolation.py`, `tests/test_cli.py`, and
  `tests/test_cli_json.py`.
- Final integrated validation was bound to source/test snapshot
  `4ceea2847f99498ebfd8367895cfeef34cb1813ff06e5d1556a696a14b3cfd46`, based
  on repository HEAD `525dc1582bf686045c05df78ed4ba22f4eb566f7`.
- Ruff check passed; format check passed across 311 files.
- The full deterministic suite passed: 1,494 passed, 3 Redis integration tests
  skipped because Redis was not configured, with 6 existing dependency warnings.
- `python -m build --no-isolation` and `twine check` passed using the existing
  build dependencies. This does not establish an isolated build.
- A fresh-virtual-environment installed-package smoke test exited 0.
  Method-governance reported `status=complete` and `rebuild_match=true`;
  diagnostics reported `status=passed` and `records_unchanged=true`.
- The wheel's 161 Python files matched `src/` byte for byte. The sdist included
  this report, and the archive paths contained no private workspace paths,
  environment files, databases, or keys.
- The final wheel members matched byte for byte with the wheel used for the
  fresh-installed-package smoke test; the sdist report matched this file.
- Tests cover repeated identical and different goals, a pre-existing fixed-name
  `final_report`, an unrelated ready task, a rejected child followed by a
  committed recovery output, root failure with a durable recovery successor,
  and SQLite reopen/rebuild equality.
- No real provider execution was performed. Documentation governance passed
  (48 tests) after the evidence update; the final sdist report was checked
  against the current file.

The same maintenance batch also includes optional batched context Asset reads;
see [Change 031](../changes/031-batched-context-asset-reads.md) and its
dedicated tests. This report makes no end-to-end performance claim for that
optimization.
