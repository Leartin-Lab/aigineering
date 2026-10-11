# Task context view: bounded development verification

## Behavior and authority

[Change 062](../changes/062-task-context-view.md) adds `aig task context ID`.
It previews current input/label disclosure and method continuation context as
separate metadata lists. The application owner uses canonical disclosure and
shares the method-context reader with Worker package construction. Exact Asset
selection plus explicit content opt-in returns only that disclosed body;
redaction remains intact, and nonpromptable or tombstoned bodies are refused.
Existing selection semantics are preserved, including metadata for such Assets
when selected. Errors omit raw exception text and private policy explanations.

The CLI uses an immutable SQLite snapshot and required Redis synchronization.
It neither publishes Candidates nor changes facts, claims, status or output
qualification. A current preview does not establish readiness, execution
authority or the package used by a historical claim. The
[operator reference](../docs/reference/task-board.md#inspect-task-context)
documents exact usage and limits.

## Source verification

The integrated working-tree map tested with the reviewed V4 installed helper
contained 542 source, configuration, test and helper files, SHA-256
`328fb015f8264372ce1af61f500cef4bd89ffb58b6115d96388664512a9746c9`.
The complete 212-file runtime map was
`a8d9b762cb43c1fb014f483c61cbc998ca012c74c71f9aa2a7632a0135e277f9`.
Both maps were unchanged after that run. This is a working tree with inherited
development work, not a committed or tagged revision.

- Whole-source Ruff and format checks passed, covering 466 Python files.
- The full serial suite passed **2211 tests**, with six dependency deprecation
  warnings. A later installed-helper-only oracle correction is verified separately;
  the runtime, tests and configuration remain unchanged from this full-suite map.
- Independent Luna code review passed 54 focused tests and a separate 22-test
  disclosure/input-binding regression run; these overlapping counts are not
  added together or to the full suite.
- Independent read-only probes verified that default metadata omits Contract
  descriptions, unrelated Assets, bodies, source URI and trace-body markers.
  Exact denied selection returned a safe error, with SQLite revision unchanged.
  Unit fixtures include trusted Store seams; they are not operational signed
  claim-writer or provider evidence.

## Installed artifact

The final 542-file map, with the installed-helper-only oracle correction, is
`915294a1b9e9a4e76099fd410aefdddae46fdcc6922ea290fc4a38c25c42da14`.
Only that helper differs from the full-suite map above; all runtime, tests and
configuration are unchanged. The final helper has separate strict success/error
envelope, revision-type and stderr privacy controls. Both maps remain bound to
their own evidence rather than treating an earlier suite as a new helper test.

Wheel and sdist builds and archive checks passed, matching all 212 runtime files
and 182 public Markdown documents, checking package privacy and resolving all
74 root guide references. Wheel SHA-256:
`f7d2ed89df9a2258fd8ade4ecbd869c35558397acb4f99c4ef205c23e63a20d3`.
An offline fresh installation outside the source checkout passed all **50 phases**
of the packaged context control against real Redis, with no Worker invocation or
provider generation. Ordinary signed Asset/task setup was used; the redacted
Asset used the normal signed publisher, not direct Store mutation.

The control covers exact metadata/body selection, redaction, denial, human
output and Redis outage. Semantic fingerprints of all 17 SQLite tables match
before and after every context query. Reopen/rebuild preserves 26 runtime records
and all table fingerprints; the complete current context response also matches
its original response after rebuilding. Source maps remain unchanged afterward.
Installed tombstone and method-continuation fixtures are explicitly outside this
control's scope; source tests exercise those reader seams separately.

The first installed attempt failed after 38 phases because its oracle rejected
the existing CLI infrastructure's fixed safe Redis preflight message. Its
original failure streams remain retained. The corrected control accepts only
that exact outage envelope; other context errors remain code-only. No application
or infrastructure behavior was changed to satisfy the oracle. Later report
packaging is checked against a fresh document map; artifact hashes are recorded
separately to avoid a self-referential report hash.

## Retained findings and limits

Two independent focused runs failed because their sandbox could not access
Redis. Their original streams were retained; explicit isolated Redis under
approved local socket access passed. This does not change the required Redis
behavior or establish a service outage.

Pre-execution helper review found initialization order, macOS directory alias,
missing post-rebuild context comparison, stderr marker coverage and strict
revision-validation gaps. Earlier evidence and findings remain separate from
later passing checks. One early author's failed static assertion lost its
original stderr bytes when that filename was reused; its hash and tool transcript
survive, and no reconstruction is presented as the original stream. A subsequent version-preservation mistake copied V4 bytes
under a V3 name, and the original V4 author reports were overwritten during its
correction. The original V3 hashes and contemporaneous review survive; a later
byte-matching reconstruction is explicitly not original evidence. Independent
V4 review, its actual packaged source and the original installed failure streams
remain available. Corrections are recorded separately rather than treating these
evidence gaps as passing preservation checks.

This slice made no provider requests and did not inspect or mutate the original
real-report Store. The [accepted real report](091-real-repository-report-acceptance-2026-10-07.md)
is evidence for its earlier frozen runtime and inputs, not a fresh model execution
on this context implementation. Captured Worker logs/tail, attachment management,
multiple boards, persistent dispatch, scheduling and remaining stable-release
gates stay open. No commit, push, tag or package publication was made.
