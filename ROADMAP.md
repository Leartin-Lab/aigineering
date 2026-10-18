# Aigineering Roadmap

## Next usability slice

The current stage delivers an agent-friendly task tool: create, claim, submit,
request independent review and export the qualified result. It must accurately
show blockers, failures and successors, provide necessary relation navigation
and machine-readable results, and resolve the shared failure-lifecycle and Redis
correctness gaps. Public Skill and command help must let an agent follow the
supported operation chain.

Backend nodes remain ordinary single-invocation tasks. Reuse signed Candidates,
shared runtime primitives and SQLite fact/claim authority; Redis remains required
collaboration infrastructure. Presentation groups only proven relations. Chat
windows, deployment conveniences, scheduled notifications, PR acceptance
adapters and complete command parity are later directions, not completion
conditions for this stage.

The immediate priority is reliable single-domain real work: execute a useful
task, expose failed and exhausted work, preserve restart/recovery, independently
accept the exact output and verify the installed artifact. Earlier accepted
report evidence remains tied to its original snapshot. Named boards and other
conveniences support that workflow; copying every reference-project command is
not a substitute for current operational acceptance.

The collaboration relay now verifies the exact SQLite event prefix before
synchronization. Explicit stream repair uses a fresh snapshot of the routed
workspace. Query generations also bind exact source history, fence concurrent
publication and refuse partial generations; explicit repair reopens the routed
source. Final scale qualification and current-artifact real-task acceptance
remain open. See [change 100](changes/100-redis-collaboration-prefix-integrity.md)
and [change 103](changes/103-query-source-binding-and-conditional-repair.md).

The current source version is **v0.5.13.dev0**. The earlier v0.5.12 snapshot
completed deterministic, package, installed-artifact, long-chain and
nested-replanning gates for local release preparation. Neither version is a published or tagged release. The
implemented ordinary workflow runs from explicit file inputs through a
readable report, task audit, and explicit failure recovery while preserving
the signed Candidate, authority, and output-acceptance boundaries. Current
implementation notes are in
[changes 033](changes/033-usable-goal-cli.md),
[034](changes/034-target-task-cli.md),
[035](changes/035-provider-output-diagnostics.md), and
[036](changes/036-provider-json-action-wire.md); integrated evidence is in
[`report 070`](reports/070-usable-cli-stabilization-2026-10-03.md).

Current live evidence includes three initial DeepSeek Flash calls: two retained
failures and one successful explicit recovery; a JSON-wire quick-goal report
that omitted required task IDs from its audit commands; a corrected
21-checkpoint chain after an earlier failed attempt and a later exact
integer-type oracle failure; and three failed nested profiles, followed by a
recovery continuation whose business output completed but whose planning graph
failed the exact recipe. A separate nested profile passed the planning graph
before claims, completed recovery from a declared conflict in synthetic
acceptance inputs, and matched the independent rebuild. All failures remain visible in the
[release evidence](reports/070-usable-cli-stabilization-2026-10-03.md).

The configured-run development slice adds the shared `aig run --config` entry,
workspace selection and explicit task-ID resume. Existing `fleet run` delegates
to the same application service. Four complete root-task examples have presets;
method lifecycle preparation and Skill-only packages retain their own roles.
See [change 037](changes/037-unified-run-entry.md) and
[report 071](reports/071-unified-run-entry-2026-10-03.md). This slice has no new
provider execution and does not inherit the earlier snapshot's live qualification.

The current development slice provides a fact-projected task board and makes Redis
required operational infrastructure: task discovery, event cursors and Worker
wakeups share application owners; normal execution and queries fail visibly on
outage. Exact result export and signed task cancel/retry are implemented. See
[change 038](changes/038-required-redis-task-operations.md) and
[task operations](docs/reference/task-board.md), with integrated infrastructure and
installed-artifact evidence in [report 072](reports/072-task-cli-required-redis-2026-10-03.md).
Development does not create a
release tag; stable qualification and tagging remain a separate closure.
One real DeepSeek Flash repository-report task now covers configured execution,
exact export, completed-root resume and reconstruction. Independent content review
found command omissions; the original output and correction evidence remain in
[report 073](reports/073-required-redis-live-report-2026-10-04.md). No independent
policy attestation or general semantic correctness is inferred.

A later real repository-report submission exposed domain-wide claim diagnostics
and submitted-state view defects. The repairs now have
[bounded integrated verification](reports/090-submitted-task-diagnostics-2026-10-07.md).
The original output was returned for content omissions. Its bounded successor
now completed ordinary review, explicit policy acceptance and exact export on
the installed runtime; see [real workflow evidence](reports/091-real-repository-report-acceptance-2026-10-07.md)
and the [Chinese report](REPOSITORY-REPORT.zh-CN.md). The original task remains
cancelled and its report unqualified; wider stable gates remain open.

Registration reconstruction verification now includes actor/key identity;
the [bounded correction](reports/094-registration-identity-materialization-digest-2026-10-07.md)
has full-suite, independent review and source-external installed controls.

Earlier Kanban-oriented development added signed comments and human holds,
review and change requests, assignment/priority/dependency management, bulk
operations, task attachments/logs, multiple boards and persistent dispatch.
The current stage focuses on making the task operation chain reliable for
agents. Scheduled starts, configured notifications and PR acceptance adapters
remain later directions; necessary Redis correctness and bounded-operation
checks remain part of current acceptance. Signed comments
operator holds before claim acquisition and active leaf-task handoff have
development verification;
policy-bound review requests and exact report decisions have
[development evidence](reports/077-policy-bound-task-review-2026-10-04.md).
Linked revisions have [development evidence](reports/078-review-linked-task-revisions-2026-10-04.md).
Expanded handoff has [development verification](reports/079-expanded-root-handoff-2026-10-04.md), with
[paired allowance returns](changes/046-paired-causal-allowance-returns.md) and
[one ordinary successor](changes/047-expanded-root-handoff.md). Persistent local
Fleet and bulk priority/retry implementations are locally integrated; see
[changes 072–074](changes/072-persistent-local-fleet-service.md),
[consistent retry results](changes/073-retry-result-consistency.md), and
[bulk priority/retry](changes/074-bulk-task-priority-retry.md). Persistent Fleet
has [bounded installed fixture evidence](reports/101-verified-task-lineage-2026-10-10.md).
The confirmed bulk missing-target defect has a
[shared owner correction](changes/081-bulk-priority-missing-target.md) and
[current installed verification](reports/102-bulk-priority-verification-2026-10-10.md)
for mixed priority/retry outcomes, retained relay-pending facts and exact replay.
The original failed priority request was recovered separately; its failure and
a later checker failure remain retained. These checks establish metadata-only
fixture behavior and do not qualify current provider productivity or the whole
stable release. Named board lifecycle
has bounded development verification; see [change 071](changes/071-named-local-boards.md)
and [report 100](reports/100-named-local-board-verification-2026-10-09.md).
Equivalent operations must preserve immutable Contracts, append-only facts,
claim fencing and independent policy acceptance rather than copy mutable card
state into the kernel.
The remaining work order is:

Single-domain real execution, observable failure/recovery and current-snapshot
independent acceptance take precedence over copying additional commands. Named
local boards are a convenience for multiple projects; they are not required to
run one useful task. The whole usability scope remains open, and command parity
alone is not a release gate.

The current lifecycle debt fixes include [Asset-read disclosure preservation](changes/075-asset-read-redaction.md),
[aggregate task-view query reuse](changes/076-task-view-query-reuse.md), and
[verified cancellation rejection results](changes/077-cancellation-rejection-results.md).
Task query reuse preserves complete task-card results while reducing repeated persisted
record scans; source-external installed CLI comparisons on 100 and 300 signed
ordinary tasks with Redis returned identical list, board and stats outputs and
left SQLite facts and schema unchanged. These are bounded fixture checks; they
add no current-provider acceptance evidence or whole-release qualification.
Cancellation retains exact audit IDs and the revision observed for each operation.
Installed signed fixtures with Redis exercised cancellation, replay and mixed
batch outcomes; retained results were checked read-only after verification-script
errors. This remains bounded CLI evidence, without Worker or provider execution.
Fleet results now include [proof-backed failure diagnostics](changes/078-fleet-failure-diagnostics.md)
and retain late slot exceptions safely. Current installed checks use one explicit
fixture Worker with real Redis and cover exact failed-claim inspection, reopen
and private-backup reconstruction. They do not establish current provider
productivity or close the full lifecycle release gates.
Pool and target runs share that exact failure-reporting owner through
[run failure diagnostics](changes/079-run-failure-diagnostics.md). Installed pool
JSON and human output were checked with Redis and local HTTP failure fixtures;
target-descendant behavior was covered by source tests. This closes a diagnostic
gap, while productive current-provider execution and the remaining lifecycle
requirements stay open.

Verified [task lineage navigation](changes/080-verified-task-lineage.md) adds
containment and recovery publication views whose source Contract already had
a canonically proved Worker invocation failure. These are source-to-publication
relations, without a unique failure-to-publication association or changes to
backend task status. The first stage has [bounded development verification](reports/101-verified-task-lineage-2026-10-10.md)
with source-external installation and native Redis. Canonical signed retry
publications now have an [exact relation reader](changes/082-retry-publication-lineage.md)
and [bounded verification](reports/103-retry-publication-lineage-2026-10-10.md).
Handoff leaf, expanded and edit successors now have an
[accepted-publication relation reader](changes/083-handoff-publication-lineage.md)
with [bounded installed verification](reports/104-handoff-publication-lineage-2026-10-11.md)
covering all eight fixture endpoints, exact replay, required Redis outage,
reopen and private-copy rebuild. The adjacent
[shared effect-container reader](changes/084-publication-effect-container-readers.md)
rejects malformed historical containers without changing commitment or replay.
These installed read controls invoked no Worker or provider. Review-linked
revisions now have an [accepted-publication relation reader](changes/085-task-revision-lineage.md)
using the same pure successor proof as handoff, with fixed per-feature effects
and issue names. [Bounded installed verification](reports/105-task-revision-lineage-2026-10-11.md)
covers the source, completed ordinary review child and ready revision successor,
full endpoint views and task cards, exact replay, required Redis outage, reopen
and private-copy rebuild. Its fixture Workers are explicit mocks; it establishes
no current-provider output qualification.
[Executable method recovery](changes/098-executable-method-recovery.md) now
preserves the original tool request through ordinary successors after a proven
invocation failure, claim expiry or admitted output rejection. The lineage
reader shares exact source and publication proof; unavailable-target references
remain explicitly unverified observations rather than runnable edges.
[Tool and callable MCP failures](changes/099-tool-mcp-failure-convergence.md)
now enter the existing claimed Worker failure path without an observation
Candidate. Successful negative business data remains an observation.
Explicit-fail and common maintenance coordination, passive-continuation
relation proof, folding and complex-task scale acceptance remain open.
These changes do not replace current-artifact actual-work acceptance or the
remaining release gates.

1. Signed conversations have [development evidence](reports/074-signed-task-comments-2026-10-04.md).
   [Operator holds](reports/075-signed-task-holds-2026-10-04.md) arbitrate against
   claims in SQLite. [Active leaf handoff](reports/076-signed-task-handoff-2026-10-04.md)
   transfers bounded unfinished work through successors. [Policy-bound review](docs/adr/ADR-031-policy-bound-task-review.md)
   adds bounded ordinary reviewer tasks and exact report decisions. [Linked revision](reports/078-review-linked-task-revisions-2026-10-04.md)
   now transfers every unqualified slot to a bounded successor with exact input
   selection and preserved policy. Exact input guards use v6; the current reader
   floor is schema21 for paired returns, prerequisite constraints and Worker
   assignments. Expanded handoff now closes
   selected descendants and continues the root obligation through one ordinary
   successor; its bounded development evidence is in report 079. Repair historical completed trees
   missing descendant cancellations, and qualify recovery scheduling against
   concurrent cancellation before stable closure. New completion transactions
   fence descendants at every level; view rebuild does not repair older trees.
2. Execution dependencies now have [implemented behavior](changes/048-task-execution-dependencies.md),
   with [bounded development verification](reports/080-task-execution-dependencies-2026-10-04.md).
   [Signed local/HTTP coordination](changes/049-authenticated-worker-coordination.md)
   now rechecks the original request against current registration and keys inside
   the writer transaction, as the prerequisite for selected-Worker claims; see
   [development evidence](reports/081-authenticated-worker-coordination-2026-10-05.md).
   [Signed assignments](changes/050-signed-task-assignments.md) now enforce
   selected-Worker claims and atomic successor inheritance; integrated
   deterministic and installed verification is recorded in report 082, while
   actual-output acceptance remains open. The observed same-ID local Worker
   registration conflict now has a
   [bounded entry repair](changes/051-local-worker-profile-promotion.md), with
   deterministic and installed [development evidence](reports/083-local-worker-entry-2026-10-05.md).
   [Injected direct-fact declarations](changes/052-injected-direct-fact-ownership.md)
   and [signed advisory priority](changes/053-advisory-task-priority.md) now have
   bounded deterministic and installed [development verification](reports/084-advisory-task-priority-2026-10-05.md).
   Priority shares application ordering and preserves claim authority; it offers
   no global fairness guarantee. [Exact hold-set promotion](changes/054-exact-hold-set-promotion.md)
   has bounded [development verification](reports/085-exact-hold-promotion-2026-10-05.md).
   [Bounded text editing](changes/055-text-edit-through-shared-handoff.md) is implemented
   through the shared immutable successor owner, with
   [integrated development checks](reports/086-task-edit-successors-2026-10-06.md) passing.
   Independent document and real-Redis control review also pass; wider Kanban
   and stable qualification remain open.
   Bulk blocking, exact hold release and cancellation, plus per-task original
   hold sets for promotion replay, have passed integrated source checks
   ([report 087](reports/087-bulk-task-lifecycle-2026-10-07.md)). Each task retains
   its own signed decision and partial results; packaged real-Redis controls pass.
   A confirmed authentication-rejection defect has also passed integrated source
   and installed fixture checks ([report 088](reports/088-authentication-rejection-isolation-2026-10-07.md)). Exact claim-attempt history and safe diagnostics
   are implemented in [change 058](changes/058-task-attempt-diagnostics.md), with
   independent review, full-suite and installed Redis controls passing
   ([report 089](reports/089-task-attempt-diagnostics-2026-10-07.md)). Current task
   context preview and exact opt-in disclosed content are implemented through a
   shared application owner ([change 062](changes/062-task-context-view.md));
   [report 092](reports/092-task-context-view-2026-10-07.md) records its bounded
   verification. Local invocation/retry capture and bounded `task logs` reading
   are implemented through one application owner
   ([change 063](changes/063-local-worker-operational-logs.md)); full-suite and
   source-external installed controls passed
   ([report 093](reports/093-local-worker-operational-logs-2026-10-07.md)).
   Full transcripts and remote log sources are
   outside that bounded coverage. Task attachment association, safe history and
   exact-byte local export use the existing signed artifact path and shared
   application owners ([change 065](changes/065-signed-task-attachment-links.md)).
   Independent integration, full-suite and outside-source installed acceptance
   passed ([report 095](reports/095-signed-task-attachment-links-2026-10-07.md)).
   Attachment unlink/retention remain planned. Named local board lifecycle is
   implemented with bounded development verification
   ([report 100](reports/100-named-local-board-verification-2026-10-09.md)); it routes ordinary services
   rather than creating another task scheduler.
   Assignment and priority pending-result recovery uses their existing signed
   publication owners ([change 066](changes/066-assignment-priority-durable-pending-results.md));
   full-suite and installed recovery checks passed
   ([report 096](reports/096-assignment-priority-pending-results-2026-10-07.md)). This is preparation
   for remaining bulk operations. Initial assignment now accepts multiple tasks
   for one Worker through the existing signed owner
   ([change 067](changes/067-bulk-initial-task-assignment.md)); full-suite and
   installed metadata/recovery checks passed
   ([report 097](reports/097-bulk-initial-task-assignment-2026-10-07.md)). Bulk
   explicit reassignment/clear uses complete exact predecessor maps through the
   same owner ([change 068](changes/068-bulk-task-reassignment.md)); full-suite
   and installed metadata/recovery checks passed
   ([report 098](reports/098-bulk-task-reassignment-2026-10-08.md)). Routine
   `--current` reassignment now captures and saves the original heads;
   `--resume REQUEST_ID` preserves the exact request through the same owner
   ([change 069](changes/069-reassignment-request-recovery.md)). Independent
   source/helper, full regression and installed metadata/recovery checks passed
   ([report 099](reports/099-reassignment-request-recovery-2026-10-08.md)).
   Bulk priority and retry have current source-external installed metadata
   verification with native Redis, relay failures and exact replay
   ([report 102](reports/102-bulk-priority-verification-2026-10-10.md)).
3. Extend persistent Fleet lifecycle coverage from its bounded installed
   fixture evidence to the remaining real-work and failure/recovery cases.
   Scheduled starts remain future work, followed by configured notifications,
   attachment unlink/retention and applicable PR acceptance adapters. Preserve
   one task/claim authority.
4. Extend actual-work evidence to current-snapshot independent acceptance and
   broader recovery cases, then finish stable-release qualification. The bounded
   repository-report loop has historical evidence for an accepted successor,
   Chinese export and installed reconstruction in
   [report 091](reports/091-real-repository-report-acceptance-2026-10-07.md).
   Current-snapshot provider execution, independent acceptance and export
   remain open; historical evidence does not qualify later source changes.
   Tag only after closure.
5. Keep controlled productivity experiments, distributed identity and schema
   migration separate from this CLI usability goal.

## Earlier local release preparation

Version: **v0.5.12**; local release-preparation runtime gates passed.

v0.5.12 is the locally verified preparation version of the single-machine
reference runtime; no package publication or tag is implied. The
documented local runtime and protocol surface do not imply an
external security audit or public-network deployment hardening.

Implemented:

- one actor-signed Candidate commitment boundary;
- SQLite-backed atomic claims, fencing, submission, facts, and trace;
- append-only runtime records and deterministic materialization rebuild;
- stateless Worker pull/package/submit protocol;
- staged planning and replanning as ordinary tasks;
- recursive causal allowance containment;
- independent output attestation;
- same-machine active-active Worker arbitration;
- Engine-as-Worker isolation and restart;
- CLI audit, replay, lineage, recovery, and task projections;
- mock and OpenAI-compatible LLM Workers;
- optional FastAPI transport;
- language-neutral signed protocol conformance vectors;
- optional Redis query projection with revision fencing, rebuild, and SQLite
  fallback;
- a signed many-to-many definition/content graph with legacy reconstruction;
- exact label-context binding in v4 Contract identity;
- advisory semantic relation publication through the Candidate boundary;
- ordinary Worker output publication as an atomic signed content,
  definition, and assertion graph;
- transactional terminal/claim closure and durable deterministic commitment
  rejection facts;
- causal allowance as the sole runtime budget source;
- one terminal-fact construction owner and Plugin-owned planning scaffold;
- planning label containment aligned across prompt, Plugin, and commitment;
- a framework-neutral signed Candidate adapter for existing agent harnesses;
- configured LLM execution as the CLI default, with mock explicit for tests;
- explicitly configured, Contract-scoped local tool registries with separate
  capability-routed ToolWorkers;
- verifiable exact slice derivations and claim-gated disclosure;
- executable AI4S literature and data-profile examples with offline replay;
- descendant-aware independent acceptance for delegated output Assets;
- deterministic JSON output shapes inherited by planned and recovery work;
- Contract-v5 separation of execution requirements from delegation scope;
- capability- and pool-routed heterogeneous local Worker fleets;
- parallel tool calls compiled into ordinary tasks plus a boolean join;
- commit-time completion convergence across concurrent SQLite writers;
- durable recovery for both projection and claim-bound structural rejection;
- a runtime-compiled AI4S example driven by one root task and a Skill;
- executable local tool contracts with deterministic input/output schemas,
  version binding, UTF-8 output limits, and descriptor-drift rejection;
- structured tool execution metadata for duration, result bytes, error type,
  and retryability;
- a read-only `task audit --json` productivity projection reconstructed from
  Contract lineage and durable runtime records;
- a runtime-only AI4S/Fleet acceptance loop covering staged planning, tool
  observation, continuation, independent `/attest`, root qualification, and
  SQLite reopen.

Release evidence is recorded in
`reports/050-post-review-boundary-hardening-2026-07-19.md` and
`reports/051-redis-query-projection-2026-07-31.md`, and
`reports/052-signed-definition-content-graph-2026-07-31.md`. v0.5.3 convergence
and harness stabilization evidence is in
`reports/053-boundary-convergence-2026-08-09.md`.
v0.5.4 AI4S and derivation evidence is recorded in
`reports/054-ai4s-auditable-example-2026-08-13.md`.
Prior v0.5.5 fleet and runtime-compilation evidence is recorded in
`reports/055-local-worker-fleet-2026-08-14.md`.
The v0.5.6 implemented boundary is described in
`changes/014-tool-closed-loop-productivity.md` and
`docs/adr/ADR-020-tool-closed-loop-productivity.md`.
Tool-closure, reconstruction, and bounded live Fleet evidence is recorded in
`reports/056-tool-closed-loop-productivity-2026-08-23.md`.

v0.5.7 adds bounded tool argument/schema validation fixes and clarifies historical
ADR implementation ownership. See `changes/015-tool-validation-patches.md` and
`reports/057-tool-validation-patches-2026-09-06.md`.

## v0.5.11 declarative method baseline

Implemented in change 021 and ADR-023:

- bounded versioned method packages and exact dependency/input binding;
- ordinary Contract instantiation with explicit tool grants;
- a seed method that creates candidate packages and revisions;
- separately declared test suites, deterministic signed assessment and independent
  qualification before exact-version reuse;
- local CLI and an explicit-fixture end-to-end example, including SQLite reopen
  and reconstruction.
- reusable deterministic claim-validation methods for exact original text and
  literal evidence bindings, plus the experimental guarded claim-review
  adapter composition with structural gating and dedicated acceptance.

Local verification is recorded in
[report 061](reports/061-declarative-method-baseline-2026-09-09.md),
[guarded review evidence](reports/065-guarded-claim-review-2026-09-13.md), and
[debt closeout](reports/066-runtime-debt-closeout-2026-09-13.md).
This is a functional baseline. Additional cases and controlled experiments on
context isolation, error propagation and heterogeneous Worker cost/quality will
prepare v0.6.0. No general productivity improvement is claimed by fixture tests.
The guarded claim-review case has native live qualification and preserved
negative evidence. Broader controlled experiments and longer chains remain
v0.6.0 preparation; the v0.6.0 release is not complete.

## Release gates

Every stable release must pass:

1. candidate/fact, authority, disclosure, claim, and terminal regression tests;
2. Memory/SQLite conformance and crash-atomicity tests;
3. materialization deletion and rebuild with matching semantic digest;
4. concurrent Worker and stale-claim fencing tests;
5. canonical signed JSON and protocol-vector verification;
6. Ruff check and format;
7. full deterministic test suite;
8. wheel and sdist build plus metadata validation;
9. installation-state CLI and database-reopen smoke tests;
10. bounded real-LLM system, Worker, and end-to-end scenarios.

## v0.5.8 evidence and operability

Implemented in `changes/016-reproducible-release-evidence.md` and ADR-021:

- backup-first reconstruction verification with retained mismatch evidence;
- reproducible signed-publication scaling measurements;
- Python 3.11–3.13 and API/Redis CI coverage plus installed-wheel checks;
- publication reuse of validated artifacts;
- AST-based dependency guards supplementing existing architecture tests.

Local observations and remote-execution limits are recorded in
`reports/058-reproducible-release-evidence-2026-09-06.md`.
The v0.5.6 diagnostic rebuild mismatch remains unexplained. Context-loading
optimization, process isolation, production MCP, and external side-effect
guarantees remain future work.

## v0.5.10 runtime primitive convergence

Implemented in `changes/018-runtime-primitive-convergence.md`:

- one canonical helper constructs new versioned Contract identities and entities;
- retry, recovery, delegation, planning, CLI and nested-Worker construction reuse
  that helper;
- administrative recovery retains the complete independent acceptance policy;
- one stateless maintenance step orders durable failure, rejection and completion
  processing across CLI, local Fleet and nested Workers;
- production code no longer depends on the legacy `core.methods` facade;
- package version metadata and outbound acquisition identification share one
  version source.

The follow-up in `changes/019-runtime-boundary-cleanup.md` adds narrow Store
capability ports, pure SQLite row materialization, canonical trace-record
encoding, complete HTTP Contract views, protocol-owned identity/signing
primitives, and core-owned derived terminal commitment without changing the
transaction or wire boundary.

The CI follow-up in `changes/020-local-concurrency-reconstruction.md` preserves
first-observation trace timestamps during rebuild and atomically publishes local
actor keys under concurrent initialization.

Local verification is recorded in
`reports/060-runtime-primitive-convergence-2026-09-09.md`. The SQLite connection
and transaction owner remains intentionally unsplit. Physical JSONL ownership
removal, incremental consequence watermarks and deeper ledger extraction remain
future refactoring slices.

## v0.5.9 portable business artifacts

Implemented in `changes/017-portable-business-artifacts.md` and ADR-022:

- bounded self-contained byte attachments through signed Asset publication;
- optional PDF/OCR and strict UTF-8 extraction with versioned page text;
- exact evidence selectors and asset-bound Markdown footnotes;
- recursive fail-closed ancestry and deterministic offline export;
- OpenAlex/Unpaywall location resolution and scoped public HTTPS acquisition;
- explicit snapshot-scoped tool registration and canonical Harness integration.

Local verification is recorded in
`reports/059-portable-business-artifacts-2026-09-07.md`. This is the first asset
and citation closure, shared by literature and other document domains. Automatic
research orchestration, authenticated full-text services, XML parsers, alternate
OCR/parser comparison, semantic claim verification, retraction checks, large blob
storage and PDF coordinate overlays remain subsequent work. No remote release
or fresh live-LLM qualification is implied by local deterministic tests.

## Candidate directions

Future candidate directions include:

- production MCP transport and protocol integration;
- process-level tool timeout, cancellation, and isolation;
- exactly-once coordination for external side effects;
- cross-machine Store and Worker discovery;
- deployment security profiles;
- reproducible productivity and quality benchmarks.
- a versioned Contract-v6 migration that makes the creation version explicit
  instead of selecting v3/v4/v5 from populated fields;
- revision-watermarked consequence processing to replace whole-log recovery and
  completion scans without introducing scheduler-owned state;
- physical removal of legacy JSONL Store code after its supported import window.

Work is not part of the supported release until its design, migration, tests,
reconstruction proof, and public evidence are complete.

## Non-goals

Aigineering is not intended to become:

- a generic prompt harness;
- a static DAG workflow engine;
- a hidden multi-agent swarm scheduler;
- a mutable conversational state container;
- a system in which Workers or tools directly write runtime facts;
- a system whose correctness depends on one Engine process remaining alive.

The v0.5.6 local tool loop does not claim production MCP transport,
process-level timeout/cancellation/isolation, exactly-once external side
effects, or cross-machine discovery.
