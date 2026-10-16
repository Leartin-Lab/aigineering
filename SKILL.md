---
name: aigineering
description: Use Aigineering to turn agent or LLM work into signed, claim-bound, replayable results with declared outputs and auditable failure. Use when an agent harness must migrate existing orchestration into trustworthy task/asset publication, when work needs durable completion evidence, or when integrating a custom pull Worker instead of returning an unstructured chat answer.
---

# Aigineering trustworthy execution

## How an agent uses it

A Contract is one independently claimable obligation; one Worker invocation is
one bounded attempt on that task. Assets hold inputs and committed outputs.
Claims reserve work, while signed Candidates submit proposed effects. A
submission is not acceptance: the Contract's frozen policy determines whether
outputs qualify. `aig run GOAL` publishes a root obligation; `aig run --task ID`
continues an existing ordinary task. Tool calls, Skills, and MCP descriptors
provide context or local capabilities; they are not task plans. Use ordinary
child tasks when work must survive an invocation.

For an existing agent harness, follow the exact loop in
[`agent-harness-migration.md`](docs/reference/agent-harness-migration.md):
create or select a task, claim one package, produce one signed action, submit
that Candidate, then inspect task status and export only qualified outputs.
`result_candidate(package, raw_action)` takes action text, for example
`'/exec ' + json.dumps({"outputs": {"report": "A concise report"}})`;
replace `report` with the exact declared output name. Submit the signed
Candidate JSON returned by `candidate_json`, not the raw action text. The
migration guide shows the complete example and supported body forms.
If independent acceptance is required, follow the exact
[review workflow](docs/reference/task-board.md#request-an-exact-independent-review)
before exporting.
When work is blocked or fails, inspect the reported blockers and exact task ID;
select a published retry or recovery successor explicitly. Start with the
[task operations reference](docs/reference/task-board.md) for commands and
failure-specific next steps.

Use Aigineering as the fact and task boundary around an agent harness. Keep the
harness's model loop, tools, memory, and internal planning; replace its mutable
shared state and self-declared completion with Aigineering packages and signed
Candidates.

## Required collaboration service

Start Redis before operational commands (`docker compose up -d redis` in the
repository). The default address is `redis://127.0.0.1:6379/0`; set
`AIGINEERING_REDIS_URL` to use another deployment. Redis is a required runtime
component for discovery, event feeds and Worker wakeups. SQLite remains the
signed fact and claim transaction authority. `aig redis status/sync/rebuild`
checks or repairs the service; `aig cache` delegates to those same commands.
Selected `aig --offline task status/audit` views support incident inspection.
Execution and mutation do not have an offline fallback.

For independent local projects, use `board create NAME`, `board list/show`,
`board switch NAME` and `board archive NAME [--restore]`. Create never selects
the new board. `--board NAME` routes one invocation; explicit `-C PATH` bypasses
saved selection and is mutually exclusive with it. Resolve relative files in
the selected workspace. Archive changes routing metadata and preserves the
selected pointer, so implicit operations block until switch/restore. Never
treat the registry or availability as task authority, readiness or acceptance.
On `board_setup_pending`, retain the exact domain/path and follow `retry_command`
from the original registry root after restoring synchronization or file access.
An already registered entry uses `create NAME --resume` to verify the exact pin
and republish unchanged routing metadata; a missing entry uses ordinary create.
Resume preserves selection/archive and rejects changed intent. Do not replace
keys or Genesis.
See [named workspaces](docs/reference/task-board.md#named-local-workspaces).

Use `task list/board/stats/show` for discovery, `task events/watch` for bounded
observation, `task cancel/retry` for signed lifecycle actions, and
`task result ID --output NAME --to PATH` for exact qualified output export.
New views use `ok/data/revision`; watch emits JSON lines. Task-wait timeout,
missing tasks and unsuccessful task terminal states return nonzero. A
`redis_sync_pending` result retains committed facts; restore and synchronize
before repeating an operation. Read the
[task operations reference](docs/reference/task-board.md).

Use `task lineage ID` to navigate the selected task's verified component across
published parent containment, recovery publications whose source Contract has a
canonically proved Worker invocation failure, exact signed retry publications,
and accepted leaf, expanded, or edit handoff publications, plus accepted
review-linked task revisions. Handoff and revision edges bind
their exact source and successor Contract publications and the shared committed
link, terminal, and declaration witnesses. Revision navigation proves the
accepted relation only; it does not re-audit the historical review decision,
report, allowance, assignment or current eligibility. Handoff navigation does
not re-audit the historical budget, tree, hold, assignment, or allowance decision.
The view does not pair an individual failure with its recovery publication.
A retry edge requires
the accepted target receipt to name its source exactly and the target to match
the canonical retry Contract; the source may have been published with other
signed effects. The read-only view keeps each Contract's own status and blockers;
it does not change eligibility or complete a group. It requires Redis and a
single captured SQLite revision. Malformed effect-type containers do not prove
published retry, recovery, handoff or revision edges. Tool continuation and
other recovery causes remain outside the verified lineage view. See [verified task
lineage](changes/080-verified-task-lineage.md), [retry-publication lineage](changes/082-retry-publication-lineage.md)
and [handoff-publication lineage](changes/083-handoff-publication-lineage.md),
plus [revision-publication lineage](changes/085-task-revision-lineage.md).

Use `task runs ID --json` to obtain the exact claim ID, then
`task diagnostics ID --claim CLAIM_ID --json` to inspect validated fact summaries.
Continue `--cursor` with the same task, claim and limit (1–100, default20).
The original revision and observation time stay fixed. Missing or conflicting
history returns nonzero with safe integrity issues. Claim allocation does not
prove invocation, and submission does not qualify output. Retry/recovery
successors use their own IDs. Raw output and exception bodies are omitted.
Actual local invocation observations use the separate `task logs` command,
described in the [operations reference](docs/reference/task-board.md#inspect-local-worker-logs).
See [change 058](changes/058-task-attempt-diagnostics.md).
Pool (`run --once`), target (`run --task`) and bounded Fleet/configured-run
failure details include the actual failed task and claim identifiers only when
exact claim history proves them; target status still describes the requested
root. Otherwise the safe unavailable status carries no identifiers. These
details are diagnostic navigation, not a retry or recovery.
Signed domain-wide claims (`run --once`) retain an explicit null selector and
one exact granted task; inspection validates the original request/receipt
chain. A diagnostic failure does not authorize rerunning a submitted producer.
See [change 060](changes/060-domainwide-claim-diagnostics.md).

Use `task context ID --json` to preview current disclosed input/label Assets and
method continuation context. Default output is metadata only. Select an exact
listed ID with `--asset ASSET_ID`; add `--include-content` only when that body is
needed. Redaction is preserved; nonpromptable/tombstoned bodies are refused.
Redis remains required. This current preview does not prove readiness or the
context of a historical claim. See [change 062](changes/062-task-context-view.md).

Use `task logs ID --claim CLAIM_ID --json` for actual local invocation/retry
observations. Select `--store PATH` for custom databases and bounded `--follow`
for streaming. Keep invocation finish distinct from signing, submission and
output acceptance; absent logs have unknown cause. Capture warnings must not
change signed execution. Arbitrary Worker stdout/stderr remains unsupported.

Use `artifact import FILE --name NAME --media-type TYPE` for a bounded signed
attachment Asset, then `task attachment add TASK_ID ASSET_ID --json`. Inspect
relations with `list` and copy verified bytes with `export ... --output PATH`.
Use `--store PATH` with the matching local identity for custom Stores.
Association preserves task inputs/context, claims and acceptance. Keep exact
IDs/key when replaying after `redis_sync_pending`; the same add synchronizes
retained facts. Export needs a fresh destination; a failed write can leave a
partial file. See [task attachments](docs/reference/task-attachments.md).

Use `task comment ID TEXT --json` and `task comments ID --json` for signed
task conversations. `--reply-to RECORD_ID` must reference a committed comment
on that same task. Comments are limited to 65536 UTF-8 bytes and do not change
task state or qualify outputs. An identical submission with the same
`--idempotency-key` returns its original record; changed input is a new Candidate.

Use `task block ID --reason TEXT`, `task holds ID`, and
`task unblock ID --hold RECORD_ID --reason TEXT` to hold unclaimed work.
Any open hold prevents claim acquisition; release exact holds individually.
Placement rejects terminal or previously claimed tasks. Unblocking an old hold
does not reopen a terminal task. Human collaboration commands support readable
output by default and `--json` for automation.

Block, cancel, unblock and promote accept up to 64 distinct task IDs; each task
has its own signed decision and the invocation can partially succeed. For bulk
unblock, provide exactly one `--hold-for TASK_ID=HOLD_ID` per task. For bulk
promotion, repeat that option for every member of each original complete hold
set; single-task `--hold` remains available. Save the returned batch base and
actual per-task keys. Repeat unchanged inputs and the base for a multi-task
replay; replay one member with its returned actual key. Promotion retains its
historical `BASE:promote:TASK_ID` keys. Changed inputs create another Candidate.
JSON preserves every target result and distinguishes accepted, rejected,
Redis-pending, unknown and unattempted work. Pending accepted results include
durable fact IDs; an audit relay pending for a rejection is not acceptance.
Any unsuccessful result returns nonzero. Restore Redis and synchronize before
continuing; inspect facts before retrying an unknown outcome.
For cancellation, a verified durable rejection includes its exact audit fact
IDs and observed revision. Untyped errors and unavailable rejection proof remain
unknown/internal outcomes; authentication-rejection claimed IDs are not proof
of Candidate identity.
Human rejection output includes its durable audit record reference. A single
post-commit pending response preserves the operation revision across later Store
writes; preflight failure returns no operation data and claims no retained facts.

Use `task promote ID... --reason TEXT` to atomically release each task's complete
observed manual hold set, and `task promotions ID` to inspect its audit history.
Read the returned status and remaining blockers; promotion cannot bypass other
eligibility checks or reset claims. Multiple tasks may partially succeed and
return nonzero. Save the returned key and exact hold IDs; an original explicit
`--hold` list plus the original reason/key replays only that prior operation.
Default discovery is a fresh snapshot. Each task supports at most 64 holds;
larger sets require partial exact unblock while keeping at least one hold open.

Use `task link TARGET --depends-on PREREQUISITE --reason TEXT` and
`task dependencies TARGET` to order new claims by exact prerequisite completion.
Copy the placement ID into `task unlink TARGET --link ID --reason TEXT`. Only
typed successful prerequisite completion satisfies an open link; failures and
cancellations retain exact blockers. Targets must remain live and never claimed.
Links do not supply input Assets or gate canonical output completion. Replace a
prerequisite safely with block→unlink→link→exact unblock; these are separate signed
decisions. No automatic recovery/successor following occurs. Handoff/revision
refuses cancelled targets with unresolved external prerequisites, while outside
downstream tasks retain old-ID blockers. Use fresh input roles/Assets explicitly
when data flow is needed. See [dependency semantics](docs/adr/ADR-036-task-prerequisite-claim-constraints.md).

Use `task handoff ID --reason TEXT` to cancel an active business attempt and declare
its bounded successor atomically. `task handoffs ID` reads durable links. Copy
the returned successor ID into `run --task ID`. Omitted `--budget` transfers the
first committed remaining allowance; exact keyed replay returns that same target.
`--hold-reason TEXT` creates a held successor; release its exact hold before
execution. The reason becomes successor Worker context. Completed outputs remain
link metadata, and their contents are not automatically disclosed. Frozen scope
and acceptance policy are inherited. Expanded handoff closes selected old descendants
and returns their unused allowance along exact funding paths; a zero source balance
can still fund one ordinary successor. It freezes source input roles and disclosure,
not child results or observations. External live independent-verifier branches remain
intact. Holds on transferred/repair nodes block with exact IDs. Missing historical
completion terminals are explicitly repaired by the signed handoff; untyped old
failure/cancellation traces block. Audit/rebuild stays read-only. The operation
refuses terminal, system/recovery or plugin-controlled/protected-minting sources.
Resolve declared source inputs before expanded handoff; unresolved roles reject
without closing the old tree. It fences
late old-claim submissions but does not stop an OS process or undo external work.

Use `task edit ID --title TEXT --body TEXT --reason TEXT` to replace one or both
instruction fields through that same bounded successor owner. Use the returned
new ID for further work; the old task stays cancelled. Omitted text is unchanged;
`--body ""` clears the body, and the reason remains in audit history. Nonblank
title and valid UTF-8 text are bounded to 65536 bytes per field; canonical no-ops
reject. `task edits ID` reads the transfer history. Edit inherits assignment and
copies the complete source hold set, up to 64, atomically with exact reasons;
descendant holds still block. Source holds stay historical and require explicit
cleanup. Repeat the original text, reason and returned idempotency key for exact
replay; the returned source snapshot can be supplied explicitly. Preserve
accepted pending-Redis results and synchronize before assuming an operation was
not committed. See [the edit workflow](docs/reference/task-board.md#edit-task-instructions).

Use `task request-review SOURCE --output NAME --asset ID --reason TEXT` for a live
independent-policy output. Run the returned ordinary reviewer task with a distinct
`run --worker-id ID`, inspect its exact report, then use
`task review decide REQUEST --report ID --verdict accept|changes --reason TEXT`.
`task reviews SOURCE` reads immutable history. Review consumes current source
allowance; its report does not itself qualify the source. Decision reuses canonical
policy attestation and requires distinct producer attribution and verifier caps.
The default supports `verify.human`; custom actor/key/file options select an
already-authorized identity without granting capabilities. Changes records rejected
attestation only. Then `task request-changes DECISION --reason TEXT` publishes a
bounded successor; copy its ID into ordinary `run --task`, review its new exact
output and qualify under the inherited policy. The source remains cancelled.
Input roles and trust checks are retained with exact input binding; later same-name
Assets cannot widen revision context. `task revisions SOURCE` reads the links.
For existing older-schema workspaces, stop old Workers and run `domain upgrade` before
other writable commands to retain a backup. The current schema21 floor refuses older readers;
already-open old connections are outside that guarantee. Ordinary sources cannot
be retrospectively gated. Older Stores containing return rows require explicit
resolution: schema18 had no supported return producer and migration refuses those
rows without reinterpreting them. Pre-20 constraint rows also refuse migration
rather than being recognized as supported history. See
[expanded handoff](docs/adr/ADR-035-expanded-root-handoff.md) and
[review semantics](docs/adr/ADR-031-policy-bound-task-review.md).

Select a registered Worker with `task assign CONTRACT WORKER --reason TEXT`.
Read `task assignments CONTRACT --json`, then use the returned exact
`head_assignment_id` in `task reassign CONTRACT --worker WORKER --previous ID`
or `--clear --previous ID`, always with a reason. A clear retains its own head;
use that ID to set again. An explicit idempotency key replays only unchanged
signed intent. Busy compatible Workers can queue assignments, but any claim
history forbids in-place reassignment. Continue claimed work by handoff/revision:
default inherits assignment; `--assignment-worker WORKER` or `--clear-assignment`
requires exact `--source-assignment-id ID` and extra assignment authority. Empty
source ID is only for a source with no history and an explicit set. Ordinary
child tasks do not inherit assignment. Pre-21 assignment rows refuse migration.
See [assignment semantics](docs/adr/ADR-038-transactional-task-assignments.md).

For assignment and priority mutations, save the returned actual idempotency key
and exact fact/Candidate IDs. Accepted `redis_sync_pending` means SQLite retained
the operation. Restore Redis and repeat the original arguments, including the
original predecessor, with that key. Preflight failure and a rejected Candidate
are distinct; neither proves an accepted operation. Prefer `--json` for recovery
metadata. See [change 066](changes/066-assignment-priority-durable-pending-results.md).

Inspect each bulk result even when the command exits nonzero. A mixed batch
containing `not_found` still exits 1 after exact replay of its accepted members.
Priority treats a genuinely absent task as not found and continues later targets;
malformed or orphan typed history remains an unknown failure that stops later
attempts. Keep the original arguments and keys for recovery. See
[change 081](changes/081-bulk-priority-missing-target.md).

For initial assignment, `aig task assign CONTRACTS... WORKER_ID --reason TEXT`
accepts up to 64 tasks for one Worker. Multiple targets return ordered partial
results and a batch base; each remains an independent signed publication.
Preserve the original Worker, reason and exact IDs when replaying. For two or
more members use the batch base; for one member use its returned actual target
key with the original single-target syntax. Unknown results stop later attempts;
relay-pending acceptance and unattempted work remain distinct. Priority and
retry remain separate bulk work. See
[change 067](changes/067-bulk-initial-task-assignment.md).

For routine set or clear, use `task reassign CONTRACTS... --current --worker
WORKER --reason TEXT` or `--current --clear --reason TEXT`. This captures every
original head and saves private complete input bytes before the first target.
Use the printed `aig -C WORKSPACE task reassign --resume REQUEST_ID` command for
recovery. Resume accepts only that ID and output formatting, preserves the entire
original intent, and never substitutes a newer head. New modes return a batch
result even for one target, including `data.request_id` and row input context.
Missing or changed files refuse visibly; no latest-request fallback exists.
Keep the same workspace location: the request binds its resolved Store path and
Genesis, and relocation/cross-workspace copies refuse. Saved requests are input
context, not authority or completion. See
[change 069](changes/069-reassignment-request-recovery.md).

For explicit several-target inputs, use `task reassign CONTRACTS...` with either
one `--worker WORKER` or `--clear`, a reason and an exact
`--previous-for CONTRACT=HEAD` per member. Copy each head from assignment
history, including a clear fact. Preserve the original maps, Worker/clear mode,
reason and batch base for recovery; use a returned actual key and the original
`--previous HEAD` for one member. The echoed `requested_previous_assignment_id`
is input context, not proof that the head was accepted. Each target remains
independently atomic, and a stale or claimed target can fail without undoing
siblings. See [change 068](changes/068-bulk-task-reassignment.md).

## Choose the integration mode

Use the built-in LLM Worker when no existing harness must be preserved. Set the
provider once; LLM is the normal execution default:

```bash
export AIGINEERING_API_KEY="..."
export AIGINEERING_MODEL="provider-model"
export AIGINEERING_BASE_URL="https://provider.example/v1"
aig run "produce a cited release review" --json \
  --input evidence=notes.md --output review.md
```

`aig run GOAL` uses the goal and only files explicitly named by repeatable
`--input NAME=PATH` options. It does not scan the workspace or fetch sources
from the network. Files must be non-empty UTF-8 and no larger than 1 MiB each.
Quick-goal runs do not load operator tools; use an existing task and an explicit
tool registry when local tool execution is required. A successful exportable
text output is written to Markdown at `--output PATH` (default:
`.aig/outputs/<session>.md`). An existing destination is never overwritten.
`aig demo` uses the built-in sample flow; MockWorker remains an explicit
dry-run/test facility.
Choose exactly one of GOAL, `--once`, or `--task`. `--input` and `--output`
require GOAL, as do `--behavior-label` and `--save-config`; quick-goal mode
rejects a tool registry and mock-output overrides.

For the configured DeepSeek Flash endpoint, keep the key in an environment
variable. `--json` selects CLI output while `--response-format json_object`
selects the provider wire format:

```bash
export AIGINEERING_API_KEY="$DEEPSEEK_API_KEY"
export AIGINEERING_MODEL="deepseek-flash"
export AIGINEERING_BASE_URL="https://api.deepseek.com"
aig run "基于 README 中的实现说明，生成中文能力、操作步骤和局限报告。" \
  --input readme=README.md --output report.md \
  --thinking-mode disabled --max-output-tokens 8192 \
  --response-format json_object
```

Use a custom pull Worker when an existing Codex, Claude Code, LangGraph, SDK,
or other harness already owns orchestration. Read
[`docs/reference/agent-harness-migration.md`](docs/reference/agent-harness-migration.md)
before implementing that loop.

Use mock only for deterministic boundary tests and dry runs:

```bash
aig run "exercise the boundary" --worker mock --json
```

Never present mock output as production or acceptance evidence.

Quick-goal `aig run GOAL` assigns a distinct output name to each invocation and
claims only work in that root task's descendant tree.
Ordinary declared outputs may still be satisfied by an intentionally shared
same-name Asset in the local domain. For `aig run GOAL --json`, use the returned
`contract_id` to read the root's `outputs`, and use the session manifest's
`produced_asset_ids` to identify Assets committed by that run. A failed root
remains terminal; run its separate recovery successor explicitly with
`aig run --task <contract_id>`.

For a target task, `aig run --task ID` claims only that Contract and its
immutable descendants. It checks an already terminal target before executing
work. `aig run --once` still claims one enabled task from the domain-wide pool;
runtime maintenance remains domain-wide for both commands. A target run's JSON
and human output include the projected status and blockers, a `stop_reason`
(`terminal`, `idle`, `timeout`, or `worker_failure`), and a suggested `next_step`.
It does not automatically execute a root recovery successor. `completed` means
the declared output obligation is satisfied; it does not establish independent
semantic truth. `--wait-timeout`, `--interval`, and provider `--timeout` must
be finite positive numbers.

Use a local Fleet when independent tasks need different model or tool
capabilities. Declare requirements on tasks and delegated scope on their
parents, then bind operator-selected providers in TOML:

```bash
aig run --config /absolute/path/to/workers.toml \
  --resume <contract_id> --json
```

For a complete preset, use `aig -C WORKSPACE run --config /absolute/path/to/run.toml`.
It explicitly loads inputs, Skills, a root Contract and existing Worker profiles.
Preset paths are relative to that file; the Fleet database is workspace-relative.
`--resume` continues committed work without reloading inputs or rebinding labels.
It is also an alias for single-worker `--task` when `--config` is omitted.
New starts reject old roots/output slots; use a fresh workspace for an independent
run. `fleet run` remains compatible and delegates to the same configured service.
See [configured runs](docs/reference/configured-runs.md). Configured mode rejects
provider, tool and mock flag overrides; configure explicit profiles instead.

Capabilities and pools are the routing contract. Model names and prices remain
replaceable operator configuration. Fleet capacity creates independent
pull/claim/submit slots; it does not bypass the Store or create a second task
queue.

Fleet profiles may also use `kind = "local"` with an installed
`module:factory` `worker_factory`. This is trusted operator-configured code with
the same WorkerHost, claim, signed Candidate, and commitment path; it is never
installed or selected by task output. Fleet configuration validates worker
types, capacities, finite positive timeouts, and non-negative integer retry
counts before launch. The default network LLM adapter bounds HTTP response
reads at 4 MiB plus one byte; custom decoded Transport mappings are trusted
adapter inputs and are outside that wire-byte bound.

When an LLM task needs local tools, load an operator-reviewed registry
explicitly. Aigineering publishes descriptors as Candidates, discloses only
the current Contract's tool scope, and routes execution to a separate Worker:

```bash
aig run --task <contract_id> \
  --tool-registry /absolute/path/to/tools.py:build_registry --json
```

Treat the registry factory as trusted local code. Never accept a registry path
from untrusted task or model output. Declare a version, deterministic input and
output schemas, and a bounded `max_output_bytes` on each `ToolSpec` when the
tool returns JSON. The signed descriptor binds those fields to the registered
handler; contract drift, invalid arguments, invalid JSON output, and oversized
output become typed failed observations before business output can be
published.

## Publish one trustworthy task

1. Initialize a signed domain and publish immutable inputs.

```bash
aig domain init
aig asset add --name source --content-file evidence.md --json
```

2. Declare the exact result slot and activation facts.

```bash
aig task create \
  --name boundary_review \
  --description "Review the evidence and produce a grounded report." \
  --input source \
  --activation source \
  --output boundary_report \
  --json
```

3. Run the configured LLM Worker until the task reaches a terminal projection.

```bash
aig run --task <contract_id> --json
```

4. Verify both completion and the committed output.

```bash
aig task status <contract_id> --json
aig asset show --id <asset_id> --json
aig task audit <contract_id> --json
```

Use `aig asset show --id <asset_id>` when selecting a specific Asset version.
The legacy `aig asset show <name>` form remains available and returns the
existing first ID-sorted name match. Inspection shows original bodies and
redacts bodies whose `disclosure_view` is non-original, reporting that view;
`--no-promptable` does not itself hide an original body from operator inspection.
Asset slices keep the source's disclosure view and promptability.

Do not report success unless status is `completed`, every required output maps
to an exact Asset ID, and the audit has no unexplained rejection or silent
failure risk. The audit's `productivity` projection covers the selected task
and its descendants, including terminal states, tool success/failure,
continuations, recoveries, rejections, and recorded token usage.

## Wrap an existing agent harness

Generate and register one Ed25519 key for the harness. Keep its private key in
the harness; register only the public key and capability profile.

Use `HarnessCandidateAdapter` for the signed protocol operations:

```python
from aigineering.agent.harness import HarnessCandidateAdapter, candidate_dict

adapter = HarnessCandidateAdapter.from_private_key_hex(
    domain_id=domain_id,
    actor_id="harness:codex",
    key_id="codex-1",
    private_key_hex=private_key,
)

claim = adapter.claim_candidate(request_id=unique_request_id)
package = post_json("/worker/claims", candidate_dict(claim))

raw_action = existing_harness(package["contract"], package["disclosed_assets"])
result = adapter.result_candidate(package, raw_action, usage_metadata=usage)
decision = post_json("/worker/submissions", candidate_dict(result))
```

The harness must return exactly one protocol action:

- `/exec` publishes only declared outputs;
- `/plan` or `/replan` publishes independently claimable work;
- `/tool`, `/parallel_tool`, `/retry`, or `/fail` makes the corresponding
  decision visible. Parallel calls become ordinary tool tasks plus an `AND`
  activated continuation.

Do not translate every internal thought, tool call, or conversational turn into
a task. Publish only work that must be independently scheduled, budgeted,
replayed, recovered, or accepted.

## Interpret projections

- `completed` is success only when declared outputs are satisfied.
- `failed`, `cancelled`, and `unreachable` are terminal failures.
- `expanded`, `ready`, `claimed`, `submitted`, `blocked`, and `stalled` are not
  success.
- A timeout is not completion; read `task status` and `task audit`.
- Submitted independent-policy work exposes eligible exact assertions in
  `qualification_pending_outputs` and an independent-acceptance blocker. Inspect
  reviews and request bounded revision after `changes`; do not rerun or reset a
  submitted claim. Those references do not grant qualification. See
  [change 061](changes/061-submitted-task-view.md).
- Aggregate `task list`, `task board` and `task stats` share one full-history
  query snapshot per command. It avoids repeated persisted fact scans while
  preserving the same projected lifecycle decisions and filters. See
  [change 076](changes/076-task-view-query-reuse.md).
- `output_truncated` and `empty_response` are durable Worker failure categories;
  inspect `task audit` and the recorded recovery before continuing.
- Any rejection requires audit before trusting downstream results.
- Missing usage after a provider failure does not prove zero external cost.
- Provider failure logs retain status and retry metadata without formatting the
  exception body. The original exception remains available to callers; inspect
  private evidence carefully before sharing it. See [change 059](changes/059-provider-log-privacy.md).
- Parent completion depends on declared outputs, not on every descendant
  reaching terminal state.

For independently reviewed work, bind an acceptance policy at task creation and
submit an attestation from a different actor. A producer cannot accept its own
output. Use `output_shapes` when JSON fields and array/item types can be checked
mechanically; the independent verifier cannot override a shape mismatch.

For a planned, retry, or recovery task with unresolved tool scope, return only
`/tool` or `/parallel_tool`. The committed observation creates a continuation
that publishes the business output. Never construct tool-derived evidence in
`/exec`, including during recovery.

## Preserve the boundary

Host extensions should use strict `EffectHandler.direct_output_fact_types` and
selective `reserved_fact_types` through the frozen injected registry. The former
is the complete direct-output set; existing `emitted_fact_types` remains assignment
metadata. Default `None` on external typed or tuple handlers is trusted legacy
compatibility, not isolation for untrusted extensions. See
[injected ownership](docs/adr/ADR-040-injected-direct-fact-ownership.md).

- Treat every Worker result as an untrusted Candidate until commitment.
- Bind claims and results to Contract, package, claim ID, epoch, Worker key, and
  a non-empty idempotency key.
- Never write `.aig/store.db` directly or reuse a signed claim command.
- Resolve labels to exact context Asset IDs at task construction; labels grant
  neither authority nor dynamic replay lookup.
- Keep child inputs, labels, tools, capabilities, outputs, and allowance inside
  parent scope.
- Treat Redis as required collaboration infrastructure whose feeds and indexes
  are rebuildable; SQLite remains fact and claim authority.
- Use `task priority` and exact heads from `task priorities` for advisory ordering.
  `list/board --sort priority` changes display order; explicit task selection and
  every eligibility/claim fence still apply. Priority does not transfer attempts
  or inherit into successors. See [the operator workflow](docs/reference/task-board.md#prioritize-eligible-work).
- Treat content identity, signed definition identity, and their assertion as
  separate. Similarity is advisory and cannot transfer authority.
- Keep private keys, sealed values, and undisclosed Assets outside prompts,
  traces, logs, and Candidate metadata.

`aig worker next` signs claims with the canonical local Worker identity when
one is available. For an externally registered Worker, pass its original signed
claim Candidate as inline JSON with `aig worker next --candidate JSON`; this
option accepts JSON text, not a file path. The CLI never guesses or provisions
a private signer for an existing foreign registration. Omit `--worker-id`,
`--contract`, or `--lease-seconds` to use the values from the signed Candidate;
explicit values must match it. A successful claim Candidate is single-use. If a
null-target scan returns no work, no request fact was committed and that same
Candidate may be retried later. For external harnesses, use the signed
`/worker/claims`, `/worker/claims/{claim_id}/renew`, and `/worker/submissions`
HTTP protocol instead of an unsigned Worker ID. Local, HTTP, Fleet, Engine,
method and harness claims all recheck the current Worker key and registration
inside the SQLite writer transaction. Recordless Store calls are trusted
internal compatibility seams, not an operational interface. See
[Worker coordination authentication](docs/adr/ADR-037-transactional-worker-authentication.md).

Authentication-rejected submissions persist audit and trace only. Their claimed
actor, Candidate ID and claim binding are untrusted; they cannot close or submit
a claim, fail a task, consume its allowance or schedule recovery. A current
valid claim remains available for an authenticated submission until its ordinary
lease/lifecycle fences intervene. Valid signed output rejection retains its
receipt-backed failure and recovery path. Old incorrect facts remain immutable;
stop older Worker processes before using the repaired ingress. See
[authentication rejection isolation](changes/057-authentication-rejection-isolation.md).

Inspect current local metadata and immutable history with
`aig worker registrations WORKER_ID --json`. An empty-domain `worker next` may
establish a pull-only identity; later concrete adapter setup can preserve that
Worker ID and key through one profile-only promotion, provided the local identity
and default routing match and no active claim exists. Repeated launches reuse
the proven promoted version. Unsupported changes require explicit operator
registration. A no-work pull still returns JSON `null`; use the history command
to inspect setup. Local hosts retain their selected version and reject after a
registration change. Stop older local Worker processes before promotion. Profile
is descriptive metadata, and adapter setup or an idle result is not model output.
See [local entry compatibility](docs/adr/ADR-039-local-worker-profile-promotion.md).

For complete domain examples, read
[`examples/literature-evidence/SKILL.md`](examples/literature-evidence/SKILL.md).
It demonstrates how a scientific workflow becomes independently claimable
retrieval, screening, extraction, synthesis, and verification tasks without
adding domain behavior to the runtime kernel. The companion
[`examples/scientific-data-profile/SKILL.md`](examples/scientific-data-profile/SKILL.md)
shows how a deterministic script Worker can disclose a bounded table profile
to later model tasks without exposing raw values.
The executable [`examples/ai4s/`](examples/ai4s/README.md) run demonstrates a
Skill-guided root task, capability-routed local Fleet, runtime-compiled child
graph, tool evidence, recovery, and SQLite reconstruction.
The experimental [`guarded-claim-review`](examples/guarded-claim-review/README.md)
composition demonstrates deterministic assessment and structural gating around
one revision generation, a separate semantic review, and a dedicated acceptor.
Literal evidence binding is structural evidence; it is not semantic truth.
