# Submitted task diagnostics: bounded development verification

## Behavior

A real repository-report Worker submitted a signed output Candidate, but the
subsequent attempt query rejected its valid domain-wide claim selector. The task
board also showed that submitted, independently unqualified work as ready.
[Change 060](../changes/060-domainwide-claim-diagnostics.md) recognizes an explicit
null selector while checking the exact original signed request, grant and receipt.
Malformed proof containers now produce an observable integrity error rather than
an exception. This remains historical inspection, not fresh Worker authorization.

[Change 061](../changes/061-submitted-task-view.md) derives submitted state from
the existing claim projection. Shared task views expose exact eligible Assets in
`qualification_pending_outputs` and an independent-acceptance blocker. A partially
qualified task lists only its remaining unqualified slots. Terminal precedence,
canonical qualification and the immutable claim fence remain authoritative.
The run guidance directs the operator to review and bounded revision.

## Exact verification scope

The integrated working-tree snapshot contained 537 source, configuration, test
and helper files. Its SHA-256 was
`c23b426a01ab5ab3d033e33c0ad8ee3bd57b678e6c1aa5546b4263ea1857ad00`;
the complete 210-file runtime map was
`03501a7bb1ae50a907415289123044554b59a28a27be86b081cd614e9c1438b3`.
Both maps were unchanged after verification. This includes inherited development
work and is not a committed or tagged revision.

- Whole-source Ruff and formatting checks passed, covering 462 Python files.
- The full suite passed: **2195 tests**, with six dependency deprecation warnings.
- Independent diagnostics review passed 67 focused tests and replayed the
  malformed receipt and terminal probes without exceptions.
- Independent submitted-view review passed 33 focused tests and 25 CLI tests
  against an isolated real Redis instance. A canonical two-output submission and
  ordinary review acceptance left only the unqualified appendix in the pending
  map. That particular unit fixture uses the trusted recordless Store claim seam;
  it is not operational signed claim-writer or provider evidence.
- A read-only inspection of the original real task recovered 17 consistent
  diagnostic events, its exact submitted claim and report Asset, and the pending
  qualification status. Semantic fingerprints of every SQLite table remained
  unchanged. No provider call or recovery write was made by this inspection.

## Retained failures and limits

The first real run generated one report with DeepSeek Flash and committed its
assertion, then failed in the attempt query. It reported 25,688 prompt tokens and
7,237 completion tokens; these are provider-reported usage, not a cost claim.
The failed stage and original output remain retained. Independent content review
requested changes because the report omitted the frozen roadmap's actual live
evidence and its acceptance limits. It has not received policy qualification.

Earlier repair drafts failed malformed-proof and partial-qualification probes.
Those failures remain separate from the final passing evidence. Some early
fixture and editor diagnostics survive only as tool transcripts or recovered
transcriptions; they do not have original private stdout/stderr files. A review
metadata timestamp that preceded production was retained as invalid chronology
and replaced by a fresh content revalidation with actual read time.

## Installed artifact

Wheel and sdist builds and Twine checks passed. Archive inspection matched every
one of the 210 runtime files and all 178 public Markdown documents to the frozen
snapshot, checked package privacy, and resolved all 72 root guide references.
The wheel SHA-256 was
`f8f704aa46b2bc27713fa3ca1d349f76a07b00572cb3fa2ca80637cd83f037ea`.
An offline fresh installation outside the source checkout passed all 48 commands
of the packaged diagnostics control against real Redis. Its Workers are explicit
fixtures, and it made zero provider generation requests. That control covers
success, failure, cursor behavior and reopen/rebuild; the real pending-policy
task inspection is separate evidence.

These initial artifacts contain the earlier checkpoint of this report. Later
documentation packaging is checked against its own complete Markdown map; archive
hashes are recorded separately to avoid a self-referential report hash.
Continuation of the ordinary review/revision workflow is recorded separately
when completed. This report does not qualify the
broader Kanban goal, a stable release, general report correctness, natural-person
review, sandbox isolation, or exactly-once external effects.
