# Expanded root handoff development verification

Date: 2026-10-04. Source version: **0.5.13.dev0**. This is development evidence for
[paired allowance returns](../changes/046-paired-causal-allowance-returns.md) and
[expanded handoff](../changes/047-expanded-root-handoff.md), following
[report 078](078-review-linked-task-revisions-2026-10-04.md). It does not qualify a
stable release or complete the continuing Kanban CLI goal.

## Implemented behavior

An active expanded business task can transfer its original unfinished obligation
to one ordinary successor even when its own available allowance is zero. The
signed handoff closes the source and selected live descendants, fences claims,
returns eligible unused descendant allowance along exact original funding paths,
and reserves the successor budget atomically. It does not copy the old execution
tree or create another scheduler. Future planning and tools remain ordinary work.

Returns debit each live donor and credit a strict original funding ancestor by the
same amount. Canonical declaration, grant/reservation, receipt and Candidate-head
provenance are checked before credits are used and again under the SQLite writer
lock. Replay cannot mint another credit. Raw Store append rejects balance-changing
allowance records; new ingress grants and reservations require canonical declared
budgets and funding provenance. Internal Python Store implementation remains
trusted; these checks do not authenticate arbitrary interpreter access or a
coordinated rewrite of local records.

Traversal reaches live grandchildren through terminal intermediaries, retaining
existing terminals. Branches serving a live outer independent policy retain their
work, holds and budget. A historically output-satisfied descendant with no typed
terminal receives explicit signed completion repair with exact output evidence;
its residual is extinguished rather than returned. Empty outputs are not proof.
Adverse untyped historical outcomes and open holds on selected or repair nodes
are visible blockers. Audit and rebuild never publish repair facts.

The successor freezes v6 exact input roles, Asset views and original scope and
acceptance policy. Missing declared source inputs block before any transfer.
Child observations, outputs and authorities are not implicitly disclosed. Original
leaf handoff identities remain unchanged. Application views now use the shared
protocol thawing owner, so nested return and output-proof mappings remain JSON
objects and arrays without changing durable fact IDs.

SQLite schema19 is a reader capability floor, with no new tables or identity
version. Pre-19 return rows or noncanonical historical funding refuse migration;
valid history retains its records and IDs. Stop old Workers and use the
backup-before-upgrade command. Already-open older connections are outside the
reader gate. See [ADR-034](../docs/adr/ADR-034-paired-causal-allowance-returns.md),
[ADR-035](../docs/adr/ADR-035-expanded-root-handoff.md) and
[operator commands](../docs/reference/task-board.md).

## Bound deterministic and independent evidence

The final 393-file runtime/test map SHA-256 is
`8323231afba00492d30face42746626e5762230dff3bb00cfa569bd47bf398c4`.
The 429-file code/configuration/vector map SHA-256 is
`9f2cb6d9905468f3bbfc765a36c85a92eb6b6201c861412caac811eded9339f0`.
Maps contain sorted relative paths and file hashes; public Markdown is checked
separately. Integrated lint and format checks passed. The serial full suite passed
**1,858 tests**, with six warnings and no skips, in 70.56 seconds.

Independent GPT-6 Luna review passed 85 focused core/plugin/migration/architecture
checks before the four application-view changes. Its 28-file evidence manifest
SHA-256 is `562305214edb3f3b99e9d43e8084edc95f427f294c4c50c0fb32f38750cf4c00`.
A separate affected review passed 26 CLI tests on the final runtime/test map;
its manifest SHA-256 is
`f472cb71066bade6b7969506c770e5cdb157c0f376140f3d073a95d4a2fbb372`.
The review delta covered four view owners and one regression test; core and plugin
files were unchanged. The two commands overlap on the handoff CLI suite. These are compositional
independent checks, not one claimed 111-test full run. Author self-checks are recorded separately.

Independent old-wheel probes preserved all 12 RuntimeRecord IDs in a canonical
schema18 history, then transferred an eligible descendant residual. An unsupported
extra grant issued through the old API refused migration without changing its
version or rows. The old wheel refused normal schema19 reopen even without returns.

Barrier tests cover competing handoffs, a new descendant reservation, claim-first,
hold-first, output completion before writer revalidation and key revocation.
These bounded interleavings are not general stress or process-termination evidence.
Three language-neutral expanded-handoff vectors cover explicit/default budgets,
a three-level funding path and replay after terminal state. Python checks projected
facts; Node checks canonical bytes, signatures, IDs, funding and conservation.
All seven collaboration vector files passed the final Node consumer. The expanded
fixtures use a root recipient and do not cover every possible funding topology.

Retained failures include the initial full-suite diagnostic mismatch; a missing
fixture parameter; invalid zero timeout; a positive timeout that allowed a fixture
child to spend its unit before handoff; and an isolated build that could not fetch
hatchling through the restricted proxy. Their corrected checks are retained along
with the originals. Fixture timing does not establish provider behavior.

## Real provider continuation and content limits

A first workspace retained one authentication failure with observable terminal
and recovery diagnostics. The operator had included the credential's surrounding
text label. Its reported usage and monetary cost are unknown. A subsequent
read-only model-list lookup is separate from completion calls. The failed domain
was preserved, and a new explicit domain was created.

One successful DeepSeek Flash planning call used 11,413 reported tokens and
allocated three child units, leaving the source with zero available allowance.
A signed expanded handoff cancelled those three live descendants, returned three
units and published successor
`task:v6:8bf0c8218df426b4c4a789b003611d195133bebabdc1f8e2e02592fa52c13074`.
The exact handoff record is
`record:a0d3dd5101a3e001cd56f12b64209df54127a53d091ab16302b625cf86457f28`.
This planning graph came from an actual provider call. The initial CLI reply had a
nested JSON display defect; durable facts were correct. The view fix and its
regressions preceded the next call.

The ordinary successor used 13,063 tokens and committed a Chinese operator note,
Asset `asset:v1:72718fa01e1645eb15faedb4b1fcf4caba10186bb7018b4442d3462f699c8d2f`.
Its export matched committed content, SHA-256
`ffb2fd86865a0b995667039d24a4464e66d4a4d68cc4d6b35a7adabb53abd5d6`.
Root and independent content review found incorrect descendant restrictions,
hold fields and fresh-workspace instructions. Completion was an ordinary assertion,
not policy-bound qualification or evidence that those instructions were usable.

A separate authorized correction obligation used one new root allowance unit,
a globally fresh output name, and v6 bindings to the exact original source, draft
and review Assets. It did not revive the completed predecessor or claim to reuse
its extinguished budget. An initial operator preparation used the wrong policy
field; that unrun task was explicitly cancelled, with zero provider calls, before
a new correctly bound task was declared.

The correction call used 16,076 tokens and produced
`asset:v1:dd1f8eb283d0a3716d1eac3ccecf50a7c3fe9e913094bdc8df8fef72b78609eb`.
Its export SHA-256 is
`e4ba6534a2e5a1784d214edf2c68e0eedc9bc65bc10ccc293edff8307cf5705f`.
Independent content review confirmed the major corrections but found a missing
v6 exact-input explanation and two overbroad independent-review requirements. A
separate human editorial derivative adds that explanation and conditions the
requirements on the frozen policy. The original committed Asset remains unchanged;
the edited guide is not another provider output or canonical policy acceptance.
Ordinary completion does not retroactively add an independent acceptance policy. All three successful provider
calls total **40,552 reported tokens**. Monetary cost remains unknown; causal
allowance is not token billing. No further call is inferred from a model-list read.

The original planning invocation used a 193-file runtime map before the view fix;
the successor and correction use runtime map SHA-256
`babbc25e91c9c32ab56680efd8b3e1b0761050ba3c75c025725c5fc0e1187646`.
Inputs were frozen public documents and the explicit draft/review, with no tools,
sealed values, credentials or private workspace paths disclosed.

Separate CLI reopens, exact replay after completion and Redis rebuild retained the
same handoff record and successor. History returned native JSON proof structures.
The source stayed cancelled and the successor completed. After the correction,
backup-first reconstruction preserved all 127 RuntimeRecords at schema19; only
derived Worker claim/registration table hashes changed as expected.

## Artifact verification and remaining gates

The final runtime wheel was built in an isolated environment using a pre-existing
offline wheelhouse, then installed into a fresh environment outside the source
tree. Separate installed CLI processes passed configured-run, normal task,
comment/hold/handoff/review/revision and expanded planning/handoff/return/replay/
rebuild/export flows, including method lifecycle checks. Expanded fixture planning
left source allowance zero; handoff cancelled three live donors and reserved three
returned units for one v6 successor, which completed and exported its output.
These installed checks used explicit fixture Workers and zero provider calls.
The installed diagnostic rebuild retained all 505 RuntimeRecords.

The wheel SHA-256 is
`d280ba4c99927d93d3fe396bf5360589a7ee8bb88774815643dd80f06d74c524`.
Its 193 runtime Python files match the frozen source. Artifact inspection passed
for the wheel and source distribution; all 145 packaged public Markdown files
matched source at the inspection checkpoint. Private paths, document references,
credentials and raw Stores were absent. The root guide's 42 direct references
resolved. Final source-distribution freshness and metadata checks must be bound to their
own artifact hash; runtime evidence stays bound to the unchanged wheel.

Assignment, priority, operational dependencies, bulk operations, boards, dispatch,
notifications and retention remain in the full goal. General historical tree
repair, the recovery-publication versus cancellation window and dedicated
cascade-cancel versus Worker-submit barrier evidence remain stable-release gates.
Current-snapshot long-chain and nested-replan qualification also remain distinct.
Cancellation fences durable submission; it does not stop OS processes or undo
external effects. No full Kanban parity, stable release, universal semantic truth
or independent natural-person review is claimed.

Changes are local only: no commit, remote branch update, tag or package publication
has occurred for this slice.
