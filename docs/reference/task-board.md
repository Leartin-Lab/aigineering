# Task board and Redis operations

Normal CLI operations require Redis. Install includes redis-py; start the
repository's local service with:

```bash
docker compose up -d redis
aig redis status --json
```

The default address is `redis://127.0.0.1:6379/0`. Set
`AIGINEERING_REDIS_URL` to select an existing deployment. No fallback to SQLite
occurs in normal application queries. `--help` remains available without Redis.

## Discover and operate work

For an agent-facing overview, start with the repository [Skill](../../SKILL.md).
This reference documents the actual task lifecycle, blockers, lineage and
recovery commands. A submitted output may still await independent acceptance;
inspect `task status` and its exact blockers before retrying or exporting.

Initialize a workspace once, then publish explicit tasks and inputs:

```bash
aig domain init
aig asset add --name source --content-file source.md
aig task create --name report --description "Write a report from source." \
  --input source --activation source --output report
aig task list --json
aig task board
aig task stats --json
aig task show <contract_id> --json
aig run --task <contract_id> --json
aig task wait <contract_id> --timeout 60 --json
aig task result <contract_id> --output report --to report.md --json
```

`list --status STATUS` is repeatable; `list/board --root ID` selects the exact
Contract and its immutable descendants. Copy complete IDs from the list. A board
groups existing projected statuses; it does not maintain editable card state.
Missing inputs and activation, capability gaps, claims, exhausted budget and
recovery successors remain observable. `show` exposes task and Contract metadata.
Runtime claims and state decisions always return to SQLite facts and fencing.
List, board and stats project from one full-history snapshot per command. The
snapshot retains actual record revisions and order and is not reused across
Stores; filtering still happens after complete card projection. This reduces
repeated persisted scans without changing lifecycle validation or promising
linear total projection work. See [change 076](../../changes/076-task-view-query-reuse.md).

`aig task lineage ID [--json]` selects the verified connected component through
exact published parent containment, recovery publications with proven source
history, canonical signed retry publications, accepted leaf, expanded, or edit
handoff publications, and accepted review-linked revisions. Supported system
tool-method recovery sources include authenticated Worker invocation failure,
claim expiration and admitted output rejection. Legacy generic recovery retains
its existing source-proof limit. See [change 098](../../changes/098-executable-method-recovery.md)
and [change 099](../../changes/099-tool-mcp-failure-convergence.md).
A handoff or revision edge binds the typed link,
source cancellation terminal and successor declaration to exact accepted
publications of both materialized Contracts. Source publication can contain
other signed effects. This proves the accepted relation only; it does not
re-audit historical handoff budget, tree, hold, assignment or allowance decisions.
A revision edge likewise does not re-audit the historical review decision,
report, allowance, assignment or present-day eligibility. Both relation types
use the same indexed receipt/head/member and exact declaration proof.
A retry edge
requires the accepted target receipt to name its source exactly, the exact
declaration/receipt/head/member proof at both endpoints, and a target matching
the complete canonical retry successor. The source declaration may be part of
a compound signed publication; the retry target must be published with only its
declaration effect. Each task keeps its own projected status and blockers.
Each supported recovery schedule is verified against its exact source record,
claim binding and accepted publication history; legacy generic recovery
retains its existing source-proof limit. Malformed or unmatched records and
recovery-unavailable reports remain unverified observations. Invocation-failure
recovery also requires its grant, failure record and release before the recovery
receipt, while normal recovery processing may record its schedule after
publication.
Human output includes receipt, declaration and head IDs for each verified edge
and labels unverified schedule observations. It requires Redis and reads one
fixed SQLite revision. The command does not group completion, readiness,
qualification or recovery scheduling. It does not prove a one-to-one failure to
publication pairing or a causal edge from a tool observation to a continuation.
Containment identifies sibling scope only. Agents can continue through the
root's ordinary claim flow or configured Fleet where supported; explicit
`/fail` remains an operator-decided terminal result without automatic recovery.
Starting from either end of
a verified edge returns the same component. See [change 080](../../changes/080-verified-task-lineage.md),
[change 082](../../changes/082-retry-publication-lineage.md) and
[change 083](../../changes/083-handoff-publication-lineage.md), plus
[change 085](../../changes/085-task-revision-lineage.md).

A canonical submitted Worker claim is shown as `submitted` and is excluded from
ready lists even while its output remains unqualified. Eligible complete outputs
under an independent policy add `qualification:independent_acceptance` and an
exact `qualification_pending_outputs` map of still-unqualified output names to
eligible Asset IDs. Already-qualified slots are omitted. Use that
map when requesting review; ordinary `outputs` retains its same-name view.
Pending qualification alone is not a missing recovery. Inspect existing review
decisions and publish a revision after `changes`; do not rerun or reset the old
claim. Terminal and expanded states retain their precedence. See
[change 061](../../changes/061-submitted-task-view.md).

`result` requires completed, consistent output obligations. It selects exact
business Assets; an independent policy requires the corresponding qualification
for that Contract and output slot. Sealed/non-promptable, tombstoned and transformed
Assets cannot be exported. Without `--to`, the command lists metadata. With `--to`,
select one output; UTF-8 bytes match the committed content, and an existing file
is never overwritten. Ordinary tasks retain their documented same-name output
sharing behavior; use isolated workspaces for independent runs.

```bash
aig task cancel <contract_id> --reason "Inputs withdrawn" --json
aig task retry <contract_id> --json
aig run --resume <new_retry_contract_id> --json
aig task audit <contract_id> --json
```

Cancellation is a signed Candidate and releases a claim through the ordinary
transaction. Retry shares the top-level `aig retry --contract ID` implementation:
it publishes a deterministic new Contract with frozen scope, not a reset of the
original claim. Inspect the returned retry ID and execute it explicitly.
Both retry spellings return the same JSON `data` fields, including the exact
Candidate, declaration fact, idempotency key and observed revision. If SQLite
commits the retry but Redis relay fails, the command exits nonzero and returns
that same exact result with `candidate_accepted: true`; after restoring Redis,
replay the same retry command to synchronize the existing Candidate. A
pre-publication Redis failure has no retry result, and a rejected Candidate
reports its exact rejection fact instead. Human success keeps the exact first
line `Retry contract created: <retry_contract_id>`; budget and original ID are
printed separately. A missing Contract from either retry
spelling returns `{"ok":false,"not_found":true,"error":{"code":"not_found","message":"Contract was not found."},"revision":null}`.
Automatic recovery successors appear in task inspection; select their exact ID
when continuing. `aig recover` remains a compatibility interface for legacy
`recovery_required` traces.

## Run cycles and retry boundaries

Without `--config`, choose exactly one of a quick GOAL, `--once`, `--task ID`, or
`--resume ID`. `--task` and non-config `--resume` target the selected Contract
and its immutable descendants. They keep cycling until the target is terminal,
the pool is idle, or `--wait-timeout` expires; `--interval` sets the polling
interval between waits. The loop does not reset a claim or publish a recovery
task. If a retry or recovery successor exists, select that exact Contract ID.

`--once` performs one pass through the local pool and includes domain-wide
maintenance. It may try configured host slots until it finds an eligible package,
then claims and invokes at most one package for the command. Maintenance may
process consequences of already recorded facts, but does not admit another
package. This is a cycle boundary, not an exactly-once provider-call guarantee;
run another command to try later work. An explicit tool registry can add a
tool-execution host, and capability routing still decides which host may claim
each task.

Configured `aig run --config PATH --once` uses the preset Workers for one bounded
cycle. Add `--resume ID` or its `--task ID` alias to select an existing target;
these forms also stop after at most one package invocation. Without `--once`,
configured runs continue using their existing run-until-terminal behavior. Do
not combine `--task` and `--resume`. Configuration rejects command-line Worker
and provider overrides; edit the explicit Fleet profile instead. See
[configured runs](configured-runs.md).

For an LLM Worker, `--max-retries` defaults to 3 and retries eligible transient
transport failures within one Worker invocation (up to one initial request plus
the configured retries). It is not a task retry and does not cap requests across
multiple Worker invocations in a target or Fleet run. `--timeout` applies to an
individual provider request; `--wait-timeout` is the target-loop deadline and
does not cancel an in-flight request. A model response may omit usage, which
remains unknown rather than zero. The default Worker is `llm`; set `--model` or
`AIGINEERING_MODEL`, and use `--base-url` or `AIGINEERING_BASE_URL` for an
OpenAI-compatible endpoint. Supply credentials through `AIGINEERING_API_KEY`
or `OPENAI_API_KEY`; never put a real key in a command example or report.

`--worker mock` is an explicit test or dry-run choice, not the production default.

## Named local workspaces

Named boards select independent local workspaces. They differ from `task board`,
which displays task states within the current domain:

```bash
aig board create research --json
aig board create engineering --json
aig board list --json
aig board show research --json
aig board switch research --json
aig domain show --json
aig --board engineering domain show --json
aig board archive research --json
aig board archive research --restore --json
```

Managed creation initializes a separate domain and private root key under
`.aig/boards/NAME`. To register an existing initialized workspace instead, use
`aig board create NAME --workspace PATH --json`. Names are 1–64 ASCII letters,
digits, underscores or hyphens, starting with a letter or digit. Creation never
selects the board. Duplicate names, canonical paths and Genesis IDs are rejected,
including archived entries.

The nearest ancestor `.aig/boards.json` is the local registry. Saved selection
routes ordinary commands; temporary `--board NAME` leaves selection intact.
Explicit `aig -C PATH ...` bypasses selection, including a corrupt registry.
Combining `--board` and `-C/--workspace` is a usage error. Management uses the
registry root and rejects global `--board`; its create `--workspace` is adoption,
while top-level `-C` chooses the management root. Relative inputs and output
paths resolve inside the chosen execution workspace. No registry/selection
retains ordinary cwd behavior. Discovery from an unrelated external cwd is not
provided, even when that workspace has been adopted elsewhere.

Switching/routing checks the existing Store's exact Genesis pin. Missing or
replaced domains and archived selected boards fail without fallback. List/show
report `availability`, alongside the separate `archived` flag; availability
does not establish Redis health, task readiness or accepted output. Archive
preserves selection and only updates registry metadata. It does not delete work,
cancel tasks, stop Workers or prevent explicit directory access. Restore the
board or switch before continuing implicit operations.

If creation commits Genesis but Redis synchronization or registry publication
fails, the command reports `board_setup_pending` with `phase`, the retained
workspace/domain ID, `registry_registered`, `facts_retained` and `retry_command`.
Restore the failed service/file access and follow that command from the same
registry root. When registration did not finish, retry the original create.
When the entry is already visible but directory durability was not confirmed,
use `board create NAME --resume` (including the original `--workspace PATH` for
adoption). Resume verifies the existing complete entry and exact pin,
synchronizes Redis and republishes unchanged metadata under the registry lock.
It preserves selection/archive and refuses changed intent or a missing entry;
ordinary create still rejects duplicate names. Neither recovery replaces the
key or Genesis. A retained key without Genesis is an explicit blocker, not an
automatic reset. Managed creation refuses an `AIG_ACTOR_KEY_FILE` targeting
another path and refuses symlinked managed workspace components.

Registry files are bounded, private and atomically replaced under a process
lock. Invalid, unsupported or unsafe registry data is not silently repaired.
The registry is configuration; normal operations still require Redis and all
task changes use their existing signed services and SQLite boundaries. This is
local workspace routing, not OS isolation or a multi-user access-control system.
See [ADR-046](../adr/ADR-046-named-board-workspace-routing.md).

## Inspect claim attempts

Read the exact task's allocation history, then copy its claim ID into diagnostics:

```bash
aig task runs <contract_id> --json
aig task diagnostics <contract_id> --claim <claim_id> --limit 20 --json
aig task diagnostics <contract_id> --claim <claim_id> --limit 20 \
  --cursor <next_cursor> --json
```

Both commands also have human-readable output. Pages default to 20 rows and
accept limits from 1 through 100. Continue with the same command, task, claim
and limit. A cursor retains its original revision and UTC observation time
across later writes, reopening and rebuilding. Pagination limits returned rows;
each inspection currently scans the Store's runtime records.

`runs` shows claim allocation and proven submission, rejection, invocation failure,
release or expiry. A grant alone does not prove a Worker invocation. `submitted`
does not qualify outputs; use task status and the frozen acceptance policy.
`lease_elapsed` means the observed lease time passed without a durable expiry
fact. The current Store grants an immutable task at most once; retry, recovery
and edit successors have separate task IDs to inspect explicitly.

Signed domain-wide claims, including `run --once`, have an explicit null task
selector and still grant one exact task. Diagnostics follow the grant's exact
request and receipt ancestry; pinned selectors must match that task. Missing or
malformed selectors and receipt fields are integrity errors, not another claim
or a reason to retry an already submitted producer. See
[change 060](../../changes/060-domainwide-claim-diagnostics.md).

Diagnostics expose exact fact IDs, revisions, valid UTC timestamps and validated causal
parents, plus recognized failure categories and reported numeric usage. Missing
usage remains unknown. Raw output, prompts, arbitrary error text and trace bodies
are omitted. Legacy or noncanonical package values display as `null`; that does
not establish that no package existed. Worker IDs require a prior valid
registration. Missing or conflicting proof returns safe integrity issues and
nonzero exit status, including when the bad history is outside the displayed page.
Unauthenticated requests' claimed IDs cannot associate them with a valid attempt.

Redis is required and outages return an explicit error. Inspection preserves
SQLite facts and claim state. These are fact summaries; actual local invocation capture is described below. See [change 058](../../changes/058-task-attempt-diagnostics.md).

Pool (`aig run --once`), target (`aig run --task`), bounded Fleet and
configured-run failures expose `worker_failure_details`. An invocation-failure
entry carries the actual failed task, claim, package, Worker, epoch and
failure/release fact IDs only when one exact claim history proves them at a
single Store revision. In target mode, the task status remains the requested
root's projection while the detail identifies the failed descendant. Otherwise
it reports `diagnostic_unavailable` without identifiers. Unexpected slot
exceptions use an unbound `slot_error`; arbitrary exception text is not
returned. Use proven task and claim IDs with the commands above to inspect
durable history. These details do not create failure facts, retry work, or
schedule recovery.

## Inspect task context

Preview the current input/label disclosure and method continuation selection:

```bash
aig task context <contract_id> --json
aig task context <contract_id> --asset <asset_id> --json
aig task context <contract_id> --asset <asset_id> --include-content --json
```

The default response separates `disclosed_assets` from `method_context_assets`.
Each entry contains only `id`, `name`, `content_type`, `origin`, `trust_tier`,
`promptable`, `disclosure_view` and `tombstoned`. It omits bodies, private source
details and policy explanations. Select an exact ID from these lists; selecting
it alone still returns metadata. `--include-content` requires `--asset` and adds
only that selected content. Redacted content remains `[redacted]`; nonpromptable
or tombstoned bodies are refused. Existing selectors can list such metadata;
this command does not change Worker selection rules.

This is a preview at the returned SQLite revision. It does not prove readiness,
authority, output acceptance or the package used by an earlier claim. Changes
before execution can change the current selection. Inspection preserves SQLite
facts and claims, requires Redis, and offers human-readable output too. Errors
return nonzero and safe codes such as `asset_not_disclosed`,
`asset_content_forbidden` or `redis_unavailable`, without raw exception text.
Signed attachment association and exact-byte export are described in
[task attachments](task-attachments.md). See
[change 062](../../changes/062-task-context-view.md) and
[bounded verification](../../reports/092-task-context-view-2026-10-07.md).

## Inspect local Worker logs

Find the exact claim with `task runs`, then read its local capture:

```bash
aig task logs <contract_id> --claim <claim_id> --json
aig task logs <contract_id> --claim <claim_id> --limit 2 --json
aig task logs <contract_id> --claim <claim_id> --cursor <cursor> --json
aig task logs <contract_id> --claim <claim_id> --follow --timeout 60 --json
aig task logs <contract_id> --claim <claim_id> --store path/to/runtime.sqlite --json
```

Quick, pool, target and configured/Fleet local runs enable capture by default.
The Store path defaults to `.aig/store.db`; a custom Store uses its own adjacent
`worker-logs` directory. Runtime invocation start, Candidate return and invocation
failure/finish are observed at the actual call boundary. The built-in LLM adapter
adds provider retry/failure status and retry counters. Arbitrary Worker stdout,
stderr, full transcripts and remote HTTP execution are not captured.

A page contains at most 100 events (20 by default). `data.truncated` means more
verified events are available for pagination. `data.follow_status` describes the
capture: `complete` means the invocation call ended, `incomplete` means a live or
unfinished prefix, and `truncated` reports a storage/event cap. An invocation can
finish before signing or submission fails, and a returned Candidate may still
need independent acceptance. None of these values is a task terminal.

JSON follow emits one compact JSON object per event plus an end envelope with
status and the last continuation cursor. Timeout or Ctrl-C stops the reader;
it does not cancel execution. EOF does not establish completion. Cursors bind
the physical Store root, domain, task, claim, capture and verified prefix; they
cannot switch captures or resume a changed prefix. Reopening preserves valid
cursor continuation. Each poll closes its SQLite snapshot before waiting.

The reader verifies the selected claim through the shared diagnostics proof,
then checks the capture header. It requires Redis and preserves SQLite facts
and claims. Missing, unsafe, inaccessible, incomplete or corrupt captures have
safe nonzero errors; a file's absence does not explain why capture failed.
Click parameter errors exit 2. A valid invocation prefix is auxiliary local
observation, not signed evidence, authority or accepted business output.

Files use private directory/file modes, exclusive creation and a hash chain for
accidental corruption detection. The chain does not authenticate local files.
Capture stops at 256 KiB or 512 events per claim, or a 64 MiB spool cap. Capture
warnings are reported separately from Worker errors and do not change normal
signed execution. Lock contention does not wait, but synchronous filesystem
operations can add latency. Restart does not overwrite an old capture. There
is no automatic pruning or retention management.

Captured fields exclude prompts, keys, headers, URLs, response/Asset/tool bodies,
raw Candidate output and arbitrary exception text. This coverage does not
sanitize custom Workers' own stdout/stderr. See
[change 063](../../changes/063-local-worker-operational-logs.md) and
[ADR-044](../adr/ADR-044-local-worker-operational-logs.md) and
[bounded verification](../../reports/093-local-worker-operational-logs-2026-10-07.md).

## Apply an operation to several tasks

Block, cancel, unblock and promote accept 1 through 64 distinct task IDs.
Each task commits independently. A later rejection leaves earlier accepted
operations intact; any unsuccessful result makes the command exit nonzero.

```bash
aig task block FIRST_ID SECOND_ID --reason "Waiting for inputs." \
  --idempotency-key block-round-1 --json
aig task unblock FIRST_ID SECOND_ID \
  --hold-for FIRST_ID=FIRST_HOLD_ID --hold-for SECOND_ID=SECOND_HOLD_ID \
  --reason "Inputs confirmed." --idempotency-key unblock-round-1 --json
aig task cancel FIRST_ID SECOND_ID --reason "Work withdrawn." \
  --idempotency-key cancel-round-1 --json
```

Unblock requires exactly one hold mapping for every selected task. Use promotion
to release a complete set of several holds on the same task. Missing, duplicate
or extra mappings reject the invocation before any target publication. The
single-task `--hold HOLD_ID` spelling remains available.

Save `data.batch_idempotency_key` and each row's `idempotency_key`, Candidate ID
and fact IDs. For block, unblock and cancel, the batch base plus action and exact
task ID derives each signed key independently of target order. Repeat the
original reason, mappings and base to replay the original batch or a subset of
two or more tasks. For one task, pass that row's actual key directly. Omitted
single-task keys retain their existing defaults, including cancellation's
trimmed-reason key. Reusing a key with changed input is a different Candidate.

JSON uses `ok/data/revision`. `data.results` preserves input order and identifies
accepted, rejected, not-found, Redis-pending, unknown and unattempted outcomes.
An accepted result awaiting Redis synchronization includes its exact durable
result and fact IDs. A rejected Candidate can also have a pending audit relay;
retained audit facts do not imply that the requested operation was accepted.
Restore Redis and synchronize before continuing. An unexpected internal failure
stops later targets, preserves earlier results, and reports the current outcome
as unknown. Inspect the returned records before deciding whether to repeat it.

For a cancellation rejection, the JSON result includes the verified rejection
data, exact audit fact IDs, `candidate_accepted: false`, `facts_retained: true`,
and the revision observed after that operation. In a batch, each verified
rejection row carries its operation revision and later targets continue. If the
rejection proof cannot be verified, the row is `outcome_unknown` and later
targets are not attempted. An authentication rejection may display a claimed
Candidate ID as untrusted audit metadata; it does not authenticate that ID.
Input validation and the required Redis preflight occur before target writes.
Human single-task rejection output includes the durable audit record reference.
A post-commit pending result retains its operation revision if another write
advances the Store before the CLI responds. A preflight failure returns no
operation data and does not claim that facts were retained.

## Task conversations

```bash
aig task comment <contract_id> "Please check the input source." \
  --idempotency-key source-question-1 --json
aig task comment <contract_id> "Source checked." --reply-to <record_id> --json
aig task comments <contract_id> --after 0 --limit 100 --json
```

Comments are signed statements with actor/key attribution, original timestamp,
and Store revision. Replies must reference an existing comment on the same task.
Nonblank text is limited to 65536 UTF-8 bytes. Completed tasks remain commentable;
comments do not change status, claims, output obligations, allowance, or acceptance.

The local operator provisions a dedicated comment actor through signed root
authorization. Worker submission authority alone cannot publish comments.
Replay the same key, text, target and reply to retrieve the existing record.
Changing any input creates a new Candidate even when the key is reused.
`comments --after` filters by durable revision, and `--limit` accepts 1 through
1000. Comment text is read from SQLite; Redis events carry record metadata.
Redis incidents use the same retained-fact/sync-pending behavior as other mutations.

## Hold work before it starts

```bash
aig task block <contract_id> --reason "Awaiting source confirmation."
aig task holds <contract_id>
aig task unblock <contract_id> --hold <hold_record_id> --reason "Source confirmed."
aig run --task <contract_id> --json
```

Each placement has its own full hold record ID. Any open hold prevents Worker
pull and direct SQLite claim acquisition. Status and board views report `blocked`
with `claim_hold:<record_id>` blockers. Releasing one hold leaves others open;
copy an exact ID from `holds`. History retains placement and release attribution,
original timestamps and Store revisions. An authorized domain operator may
release a hold created by another authorized actor.

Placement rejects tasks with terminal or claim history. It does not pause a
running Worker. Use the successor handoff workflow below for active business work.
Terminal tasks keep their outcome; `unblock` may clean up an old hold without
reopening them. Hold reasons are nonblank UTF-8 text up to 65536 bytes.

Add `--json` for `ok/data/revision` output. `--idempotency-key` enables exact-input
replay with the original durable record, including after later release, claim or
terminal closure. A changed input creates a different Candidate. A different
Candidate trying to release an already closed hold is rejected. Hold changes are
single-effect signed Candidates. SQLite arbitrates claim/placement and competing
releases under its writer lock; a pending Redis relay never resets a claim.

## Recover manually held work

```bash
aig task promote CONTRACT_ID --reason "All manual prerequisites are resolved." --json
aig task promotions CONTRACT_ID --json
aig task promote FIRST_ID SECOND_ID --reason "Release the observed manual holds." --json
```

Promotion snapshots all currently open manual holds on each task and releases
that exact set atomically. A concurrent new hold or competing release rejects
the complete task operation. Dependencies, activation, missing input Assets,
assignment and Worker capacity still apply; the result reports observed status
and remaining blockers rather than promising that the task is ready.

Save the returned `hold_ids`, reason and `idempotency_key`. To replay the original
operation, repeat every exact hold ID and the original key:

```bash
aig task promote CONTRACT_ID --hold FIRST_HOLD_ID --hold SECOND_HOLD_ID \
  --reason "All manual prerequisites are resolved." --idempotency-key ORIGINAL_KEY --json
```

The replay returns the original audit and releases, plus current blockers. A
later hold stays open. Default discovery is a fresh snapshot operation. Empty
hold sets, terminal tasks, any claim history and satisfied nonempty outputs
reject fresh promotion; use exact `unblock` for partial release or historical
terminal hold cleanup, and retry/recovery for new attempts.

Up to 64 tasks may be supplied. Each task is atomic, but the whole invocation
can partially succeed; JSON lists each result and any failure returns nonzero.
`--hold` is available only with one task. Up to 64 holds can be released per
task. For a larger set, release some exact holds with `unblock`, retain at least
one open hold, and then promote the remaining complete set. The domain operator
needs `task.unblock` authority. Redis outages preserve accepted SQLite facts and
report synchronization pending. See
[ADR-042](../adr/ADR-042-exact-hold-set-promotion.md).

For several tasks, supply each original complete hold set with repeated
`--hold-for TASK_ID=HOLD_ID` options:

```bash
aig task promote FIRST_ID SECOND_ID \
  --hold-for FIRST_ID=FIRST_HOLD_ID --hold-for FIRST_ID=OTHER_FIRST_HOLD_ID \
  --hold-for SECOND_ID=SECOND_HOLD_ID --reason "Manual checks complete." \
  --idempotency-key promotion-round-1 --json
```

Every selected task needs a mapping. Each set commits atomically within its own
task; the invocation can partially succeed. Multi-task promotion retains the
`BASE:promote:TASK_ID` key derivation. Repeat the original complete mappings,
reason and base for replay, or use one task's returned actual key directly.
Without mappings, promotion discovers the current open sets and is a fresh
operation; it cannot reconstruct an earlier set after later holds appear.

## Order work with exact prerequisites

```bash
aig task link <target_id> --depends-on <prerequisite_id> \
  --reason "Prepare the source before this task starts." --idempotency-key order-1
aig task dependencies <target_id> --json
aig task status <target_id> --json
aig run --task <prerequisite_id> --json
aig run --task <target_id> --json
```

The prerequisite must have a typed successful completion fact before a new
claim on the target can succeed. Failed, cancelled or unreachable prerequisites
remain blockers; missing typed completion is also insufficient. History shows
the exact prerequisite, observed terminal, attribution and placement ID. A link
can remain open after it is satisfied; open does not necessarily mean blocked.
Already-completed prerequisites can be linked as auditable satisfied edges.

Both endpoints must exist. The target must be live, never claimed and have no
already-satisfied nonempty output obligation. Self-links, duplicate open pairs
and cycles reject, including cycles through already-satisfied open links. Only
an explicitly authorized dependency operator publishes links; ordinary Worker
submission authority does not grant this permission. Reasons are nonblank text
up to 65536 UTF-8 bytes. Add `--json` for automation.

Links order claims; they do not pass prerequisite outputs into the target, change
frozen inputs or acceptance policy, or prevent canonical completion from existing
business Assets. Declare the target's input roles and exact Asset bindings
separately. Completed or previously claimed targets cannot be edited or reset.

To replace a failed or cancelled prerequisite before claim, fence the multi-step
operation with a manual hold:

```bash
aig task block <target_id> --reason "Replace the prerequisite safely."
aig task unlink <target_id> --link <old_placement_id> --reason "Use the new attempt."
aig task link <target_id> --depends-on <new_prerequisite_id> --reason "Wait for this attempt."
aig task unblock <target_id> --hold <exact_hold_id> --reason "Replacement recorded."
```

Copy full IDs from the returned history. Unlink removes one exact open placement,
leaving all other constraints and holds effective. These commands are separate
signed decisions rather than an atomic replacement. Identical inputs and replay
key return the original fact after release, claim or terminal changes. Changed
signed inputs create a different Candidate and undergo current validation.

Retry/recovery/handoff does not rewrite an edge's prerequisite ID. Outside
downstream targets keep cancelled-old-ID blockers and can be explicitly relinked
to the selected successor. Handoff/revision refuses transfer of a cancelled target
with unfinished external prerequisites; internal old edges remain history and
repair-only canonical completion remains allowed. No dependency graph cascade
cancellation is introduced.

Schema20 makes normal older-reader reopen fail even without dependency records.
Stop old Workers and use `domain upgrade` to retain a backup. Migration refuses
pre-20 constraint imports rather than silently adopting them; the old Store and
backup remain available. Already-open old readers are outside the gate.
See [ADR-036](../adr/ADR-036-task-prerequisite-claim-constraints.md).

## Hand off unfinished work

```bash
aig task handoff <contract_id> --reason "Continue with the corrected source." \
  --hold-reason "Await source confirmation." --idempotency-key source-handoff-1
aig task handoffs <contract_id>
aig task unblock <successor_id> --hold <hold_record_id> --reason "Source confirmed."
aig run --task <successor_id> --json
```

One signed operation cancels the old attempt, releases its active claim and
declares a bounded successor in the same transaction. The old task stays cancelled;
late submissions under its claim cannot commit. Cancellation does not stop the
process or reverse external effects. A held successor cannot be claimed until its
exact hold is released. Omit `--hold-reason` to create ready successor work.

Omitted `--budget` transfers current remaining causal allowance; for an expanded
source this includes eligible unused descendant balances returned along their
original committed funding paths. A zero source balance can still fund its
successor. Previously extinguished balances and preserved external-verifier
branches are excluded. The return credits the source and debits its donor once;
it never grants another root budget. An explicit
positive budget may transfer less. Untransferred residual allowance is extinguished.
The signed null request stays unchanged on replay: the same key and inputs return
the original link and target even after later execution or terminal closure.
Changed inputs produce a different Candidate. History retains actor/key attribution,
original time, revision, completed slot names and retained exact Asset IDs.

Only unfinished outputs are declared, with the predecessor's frozen tools,
inputs, labels, context, worker/delegation restrictions and acceptance policy.
The explicit reason becomes successor description and Worker context. Retained
Asset contents are not automatically added to inputs or prompts. Ordinary tasks
retain global same-name business-output matching: retained IDs are not proof
that this predecessor produced them. Independent-policy tasks require exact
qualification, and predecessor acceptance cannot qualify successor outputs.

The operation supports active business tasks, including expanded roots and planner
output tasks. It closes selected unfinished descendants and transfers the original
root goal to one ordinary successor; it does not copy the old execution tree.
The successor can plan and continue normally. Branches serving a live independent
ancestor outside the old root are preserved with their descendants and budgets.
A reviewer serving only the old root is closed; new output needs fresh review.

Holds on transferred or completion-repair nodes block with exact Contract/hold IDs.
Release those holds explicitly, then retry with the same intended inputs. Existing
terminal intermediaries remain unchanged and do not shield live grandchildren.
For a descendant with nonempty satisfied outputs but no terminal fact, signed
handoff explicitly records completion, closes its claim and destroys its own unused
allowance. Its exact output proof appears in history. Untyped old failure/cancellation
traces block with exact IDs rather than being silently rewritten. Audit/rebuild
does not perform that repair.

Source eligibility still excludes terminal, system/recovery, plugin-controlled
and protected-minting tasks. Expanded successors freeze the source's legal inputs
and labels with exact Asset bindings; child results or observations are not added
to prompts. Resolve every declared source input before expanded handoff; unresolved
input roles reject without closing the old tree, rather than freeze an unusable
empty binding. Later same-name Assets cannot expand the successor's input selection.
Old leaf identities and replay remain unchanged. Policy-bound revision
uses the separate changes-decision workflow below and still requires no unfinished
descendants.
Use `--json` for automation; default output includes full IDs and a copyable
unblock command. Rejections are durable; a Redis outage may leave synchronization
pending, with acceptance reported separately.
If runtime polling observes a late rejection on an already cancelled, complete
or unreachable task, it records `terminal_preserved` and unavailable recovery;
the existing terminal result remains unchanged. Failed tasks keep their normal
recovery path. Handoff events are scoped to both source and successor. Event
reads are paginated: continue from `next_revision` rather than assume the first
default page contains the full task history.

Current workspaces use schema21. Stop old Workers and use
`aig domain upgrade --json` before other writable commands to retain the upgrade
backup. Ordinary writable opens also migrate automatically. Schema18 has no
supported return producer; any preexisting returned row blocks migration with
the original Store retained. Upgrade also checks historical funding facts against
canonical declared budgets and committed provenance; unsupported extra grants
block with the old version and records retained. Already-open old connections are outside the reader
gate. See [allowance returns](../adr/ADR-034-paired-causal-allowance-returns.md) and
[expanded handoff](../adr/ADR-035-expanded-root-handoff.md).

## Edit task instructions

```bash
aig task edit CONTRACT_ID --title "Updated title" --reason "Correct the brief." --json
aig task edit CONTRACT_ID --body "" --reason "Remove obsolete instructions." --json
aig task edits CONTRACT_ID --json
```

Supply one or both replacement fields. Omitted text stays exactly unchanged;
an empty body is allowed, while titles must be nonblank. Each field is valid
UTF-8 and at most 65536 bytes. Canonical Unicode NFC no-ops reject before edit
identity setup. The reason is signed audit metadata and is not appended to
the successor's instructions.

Use the full returned successor ID for further work. The old task is cancelled
and its claim fenced; its Contract and terminal history remain intact. The shared
handoff owner supports eligible active and expanded business tasks and keeps its
output, allowance, input, disclosure, independent-verifier and adverse-history
restrictions. Terminal, system/recovery, plugin-controlled, protected-minting
and externally blocked transfers reject. New outputs need fresh qualification.
Assignment inherits from the exact captured source head; priority does not.

The complete source hold set, up to 64, is copied with exact reasons in the same
transaction. The result contains source/copy IDs and declaration references.
Old source placements stay historical and may still appear open; releasing them
does not release successor holds. Descendant holds still block editing. For
more than 64 source holds, explicitly unblock some placements first and then
edit the remaining complete set. Editing never copies a partial snapshot.

The actual idempotency key, source assignment head and sorted source hold IDs
are returned. Repeating the original text, reason and key authenticates the
original request, including after old-source hold cleanup. To supply that
snapshot explicitly, repeat every original hold ID in the returned order:

```bash
aig task edit CONTRACT_ID --title "Updated title" --reason "Correct the brief." \
  --source-assignment-id ORIGINAL_HEAD_ID --source-hold-id FIRST_HOLD_ID \
  --source-hold-id SECOND_HOLD_ID --idempotency-key RETURNED_KEY --json
```

Use an empty assignment value if the original head was absent. The CLI's keyed
replay can recover the original empty hold set; an omitted unkeyed hold list
captures the current complete set. Changing the original request under its key
rejects. Current authorization is still required, including `task.edit` and
`task.block` for copied holds; revoked keys are not reprovisioned. Redis is
required. A `redis_sync_pending` result with `candidate_accepted=true` retains
the successor and returns its durable data: synchronize and inspect before
publishing another request. History reads cannot complete work. See
[ADR-043](../adr/ADR-043-text-edit-through-shared-handoff.md).

## Inspect local Worker registration

```bash
aig worker registrations WORKER_ID --json
```

This required-Redis operational read returns current metadata, active-claim
count, full registration history and profile promotion records with exact fact
IDs. It does not provision keys or write SQLite facts. Missing identities return
an observable error. A no-work `worker next --json` retains its `null` result;
use this separate command to inspect the identity it established or loaded.

A canonical local pull-only registration can advance once from an empty profile
to a concrete adapter profile under the same Worker ID and key. Only profile and
the next immutable version may change; default routing, enabled state, exact
history and current signer must match. An active claim blocks promotion, even
when its lease has elapsed but has not been closed. Unsupported changes require
explicit authorized registration. Repeated default launches reuse the exact
committed promotion without adding facts; prior hosts reject after a version
change.

Stop older local Worker processes before promotion. Profile is descriptive
compatibility metadata, not a capability grant or proof of executed code.
Ordinary explicit registration and external signed commands remain unchanged.
Adapter setup and idle scans do not establish real model productivity. See
[ADR-039](../adr/ADR-039-local-worker-profile-promotion.md).

## Assign exact work

Before selecting an execution identity, inspect its immutable registration using
the registration command above. Assignment names a Worker ID; profile promotion preserves
that ID and does not change the task's acceptance policy.

Register the desired Worker, then select it for one exact live, never-claimed
Contract. A reason is required. Busy compatible Workers may receive queued work;
the task stays blocked until that Worker's capacity becomes available.

```bash
aig task assign CONTRACT_ID WORKER_ID --reason "Route this task to the research Worker." --idempotency-key assign-1 --json
aig task assignments CONTRACT_ID --json
aig task reassign CONTRACT_ID --worker OTHER_WORKER_ID --previous ASSIGNMENT_ID --reason "Use the alternate Worker." --idempotency-key replace-1 --json
aig task reassign CONTRACT_ID --clear --previous CURRENT_ASSIGNMENT_ID --reason "Allow ordinary compatible routing." --idempotency-key clear-1 --json
```

Single-target mutation JSON uses `ok/data/revision`; `data.assignment_id` is the durable fact
ID. History returns `head_assignment_id`, nullable effective `worker_id`, a
blocker and oldest-first `history`. A clear is an explicit fact, so use its head
ID to set again with `reassign`. Initial `assign` is for no-history tasks.

Assign up to 64 tasks to the same Worker with the same command. Put the Worker
after all Contract IDs; `--reason` remains required:

```bash
aig task assign FIRST_CONTRACT SECOND_CONTRACT WORKER_ID \
  --reason "Route these tasks to the same Worker." --json
```

One Contract retains the original single-target result and direct key. Multiple
Contracts return ordered `data.results`, counts and a `batch_idempotency_key`.
Each task publishes independently through the same assignment owner; a refused
target does not undo accepted siblings. Queued placement does not bypass Worker
capacity at claim time. Invalid global input is checked before Store, identity
or Redis access. Reassignment uses explicit predecessor maps as described below.

Save the batch base and each result's actual `idempotency_key`. Replay two or
more original members, in any order, using the same base, Worker and reason.
To recover one member, use the single-target command with its returned actual
key; passing the batch base to a singleton does not replay that member.
Accepted relay-pending rows retain their exact Candidate and fact IDs. A sustained
relay failure can leave later assignments `not_attempted`; after restoring Redis,
replay committed members and continue the remaining work. An unknown outcome
stops later calls, including accepted-pending responses without an exact result.
Human output avoids echoing unsafe batch bases; JSON preserves exact keys.
See [change 067](../../changes/067-bulk-initial-task-assignment.md).

For routine operations, capture all current heads automatically and retain the
original request:

```bash
aig task reassign FIRST_CONTRACT SECOND_CONTRACT --current --worker OTHER_WORKER_ID \
  --reason "Move these queued tasks to the alternate Worker." --json
aig task reassign FIRST_CONTRACT SECOND_CONTRACT --current --clear \
  --reason "Restore compatible routing." --json
aig -C WORKSPACE task reassign --resume REQUEST_ID --json
```

`--current` accepts 1 through 64 tasks, including clear heads, and requires an
assignment history for every member. It prepares all heads on one immutable
snapshot and saves the complete request before attempting any target. It prints
the shell-quoted workspace resume command to stderr; JSON stdout remains one
object. New modes use a batch result even for one member, with `data.request_id`,
per-row request ID and the original requested head. Existing explicit singleton
forms retain their original result shape and direct replay keys.

`--resume` accepts the original ID and `--json`; it rejects targets, reason,
Worker/clear, key and head overrides. Already committed members replay original
facts under current authorization, while an unattempted member whose head changed
refuses. Start a deliberate new `--current` command to request new intent. Known
partial failures and Redis-pending outcomes follow the same collection rules as
explicit batches. A no-history/unknown task prevents initial request preparation;
use `task assign` for first assignment.

Private canonical request files live under `.aig/requests/reassign/`, with a
1 MiB file bound. They bind exact original inputs, Store path, Genesis and observed
revision, and have no authorization, task progress or acceptance meaning. Loading
checks bytes/digest/schema and private bounded regular files, then confirms
file/directory durability before recovery. Missing, edited, noncanonical or
symlink files refuse. Hash addressing is integrity for the named inputs, not a
signature or protection against coordinated local replacement. A copied Store
at another path or relocated workspace cannot automatically resume the old ID.
See [change 069](../../changes/069-reassignment-request-recovery.md) and
[verification scope](../../reports/099-reassignment-request-recovery-2026-10-08.md).

For explicit input selection, replace or clear assignments for 2 through 64 tasks with a complete map of
their exact current heads. Apply one replacement Worker or one clear action to
the batch:

```bash
aig task reassign FIRST_CONTRACT SECOND_CONTRACT --worker OTHER_WORKER_ID \
  --previous-for FIRST_CONTRACT=FIRST_HEAD \
  --previous-for SECOND_CONTRACT=SECOND_HEAD \
  --reason "Move these tasks to the alternate Worker." --json
aig task reassign FIRST_CONTRACT SECOND_CONTRACT --clear \
  --previous-for FIRST_CONTRACT=FIRST_HEAD \
  --previous-for SECOND_CONTRACT=SECOND_HEAD \
  --reason "Restore compatible routing for these tasks." --json
```

Copy each head from `task assignments`; a clear fact is a valid nonempty head
for a later set. No-history tasks use initial `assign`. Every batch target needs
exactly one mapping; duplicate, missing, foreign or blank heads reject before
Store, local identity or Redis access. `--previous` remains single-target only,
and `--previous-for` is for multiple targets.

Ordered results include `requested_previous_assignment_id` on every row as
input context. Accepted data and fact IDs separately prove the actual committed
transition. Stale-head or other known refusals preserve successful siblings.
Keys bind the base, target and exact predecessor, so reordering and subsets
retain keys; a changed predecessor is a new intent. For recovery preserve the
original maps, mode, reason and base. One member uses its actual returned key
with the old single syntax and original `--previous`, even if a later head now
exists. Exact replay still uses current authorization and original provenance.

Accepted pending with exact data continues to the next callback. If the outage
persists, ordinary pre-sync prevents its publication and reports `not_attempted`;
if relay recovers, that target can proceed. Unknown failures and accepted
pending without exact result stop later calls. Human output withholds unsafe
batch bases; JSON retains exact values. See
[change 068](../../changes/068-bulk-task-reassignment.md).

Unknown, disabled or incompatible Workers, stale predecessors, redundant clear
and any claim history reject. Exact replay needs unchanged arguments and the
same explicit idempotency key; omitting the key creates another attempt.

Successful single-target `assign`, `reassign`, and `priority` results include
`data.idempotency_key`, `data.candidate_id` and `data.fact_ids`. Save the actual
key even when you omitted `--idempotency-key`. If SQLite accepts the operation
but relay fails, the command exits nonzero with `error: "redis_sync_pending"`,
`candidate_accepted: true`, `facts_retained: true`, exact `data`, and its observed
`revision`. Restore Redis and repeat the original command with that returned
key. For `reassign` and priority updates, retain the original `--previous` value;
do not substitute the latest head. Exact replay returns the original fact IDs.

A failure before publication has no accepted operation data. A retained rejected
Candidate may have pending audit synchronization, with `candidate_accepted:
false` and exact rejection IDs; that is not an assignment or priority change.
Use `--json` for structured recovery metadata. See
[change 066](../../changes/066-assignment-priority-durable-pending-results.md).

The selected Worker is checked inside SQLite claim acquisition, across local,
harness and HTTP entry points. Assignment does not change frozen inputs,
authority, allowance or output qualification. Board/status show its effective
Worker and blocker; disabling a Worker does not silently select another.

After claim, use a successor rather than reassigning the original attempt.
Handoff and review-linked revision inherit the effective Worker atomically.
An explicit override also requires assignment authority:

```bash
aig task handoff CONTRACT_ID --assignment-worker OTHER_WORKER_ID --source-assignment-id CURRENT_HEAD_ID --reason "Continue on the alternate Worker." --json
aig task handoff CONTRACT_ID --clear-assignment --source-assignment-id CURRENT_HEAD_ID --reason "Continue with ordinary routing." --json
aig task request-changes DECISION_ID --assignment-worker OTHER_WORKER_ID --source-assignment-id CURRENT_HEAD_ID --reason "Revise on the alternate Worker." --json
```

The source ID must be its exact assignment head, including clear. For an explicit
set on a source with no history, pass `--source-assignment-id ''`. Explicit clear
of an unassigned source rejects. The old claim stays released or submitted;
it is not transferred or reset. Ordinary child tasks do not inherit assignment.
See [ADR-038](../adr/ADR-038-transactional-task-assignments.md). Pre-21 assignment
rows are unsupported imports and block upgrade. Redis schema-v2 events include
typed `worker_ids`, including causal references to previous assignees. Read the
task's assignment head for its current Worker; event indexes do not grant claim
authority, including after scoped rebuild.

## Prioritize eligible work

```bash
aig task priority CONTRACT_ID 80 --reason "Finish this report first." --idempotency-key urgent-1 --json
aig task priorities CONTRACT_ID --json
aig task priority CONTRACT_ID -10 --previous PRIORITY_FACT_ID --reason "Defer this task." --json
aig task priority CONTRACT_ID 0 --previous CURRENT_PRIORITY_FACT_ID --reason "Clear the hint." --json
aig task list --sort priority --json
aig task board --sort priority
```

Copy full head IDs from `priorities`. Priority is an integer from -100 to 100;
higher values are considered first. A task without history has effective value
zero and no synthetic fact. Clearing to zero retains an explicit head, which
must be named by the next update. History includes the exact predecessor,
actor, key, Candidate, original timestamp and Store revision. Mutation JSON uses
`ok/data/revision`, with the new fact in `data.record_id`.

For several tasks, supply every exact predecessor explicitly:

```bash
aig task priority TASK_ID_1 TASK_ID_2 80 \
  --previous-for TASK_ID_1= --previous-for TASK_ID_2= \
  --reason "Raise these tasks" --idempotency-key priority-batch --json
aig task retry TASK_ID_1 TASK_ID_2 --json
```

An empty predecessor (`TASK_ID=`) means that task has no previous priority
fact. Each target publishes independently and the response retains all target
outcomes. Retry preserves each task's deterministic successor and Candidate
key; its batch correlation ID is not a publication key. Known not-found and
typed task-state rejections do not prevent later targets from running. An
unexpected error stops later calls, which are reported as `not_attempted`.
Neither operation resets a claim or changes task eligibility. See
[change 074](../../changes/074-bulk-task-priority-retry.md).

A mixed batch still exits 1 when a target is not found, including after exact
replay of all accepted members; inspect the ordered JSON results rather than
using process exit alone to infer which publications succeeded. Priority uses
`not_found` only for an absent Contract with no exact declaration or priority
history. Orphan typed history and other integrity failures remain unknown and
stop later attempts. See
[change 081](../../changes/081-bulk-priority-missing-target.md).

New changes reject terminal tasks, satisfied nonempty output obligations,
no-ops and stale or missing predecessors. An active claim continues unchanged.
Identical signed arguments and the same explicit idempotency key return the
original fact after later edits or terminal closure, provided the current
operator key is still valid. Reusing a key with changed intent cannot bypass
current validation. Invalid arguments are checked before operator provisioning.

Local broad scans and existing scoped run, fleet and engine scans share one
application ordering service: priority descending, original declaration revision
ascending, then full ID ascending. An explicitly selected task remains pinned.
Assignment, dependencies, holds, activation, capabilities, capacity and claim
fences still determine eligibility, so blocked urgent work can yield to ready
lower-priority work. List and board keep their existing default order unless
`--sort priority` is selected; status and show expose the effective value and head.

This is advisory ordering within one scan snapshot. Concurrent processes can
claim in a different order; global FIFO, fairness, aging and start times are
not guaranteed. High-priority arrivals may starve other work. Retry, recovery,
handoff, revision and children do not inherit priority. Redis carries event
references and wakeups; SQLite remains the fact and claim authority. Schema21
does not change, and older readers can ignore this hint. See
[ADR-041](../adr/ADR-041-advisory-task-priority.md).

## Request an exact independent review

Declare an independent acceptance policy before execution, using frozen rubric
and evidence Asset IDs when the policy requires them. For example, save this
policy as `policy.json`, replacing the placeholders with IDs returned by
`asset add`. These snippets require actual input files, configured provider
credentials and full returned IDs; they are not a single paste-ready script.

```json
{
  "mode": "independent",
  "policy_version": "report-review-v1",
  "required_attestations": 1,
  "verifier_capabilities": ["verify.human"],
  "rubric_asset_ids": ["<rubric_asset_id>"],
  "evidence_asset_ids": ["<evidence_asset_id>"]
}
```

```bash
aig task create --name reviewed-report --input source --output report --budget 5 \
  --acceptance-policy "$(cat policy.json)" --json
aig run --task <source_id> --worker-id producer-1 --json
```

The producer run can return nonzero because independent qualification is still
pending even though its output committed. Inspect its JSON `outputs`, submission
status and failure details. Use the exact committed target ID for the request:

```bash
aig task request-review <source_id> --output report --asset <target_asset_id> \
  --reason "Check the cited evidence." --budget 1 --idempotency-key review-1
aig run --task <reviewer_task_id> --worker-id reviewer-1 --json
aig task result <reviewer_task_id> --output <report_output_name> --json
aig task review decide <request_record_id> --report <review_report_asset_id> \
  --verdict accept --reason "Checked the exact target against the frozen rubric." \
  --idempotency-key review-decision-1
aig task reviews <source_id> --json
```

Copy the exact report Asset ID from the reviewer run's JSON `outputs` and check
its producer against the reviewer task. `task result` inspects the named outputs.
Use `asset show --id <asset_id>` when the exact version matters; the legacy
`asset show <name>` form remains available and selects its existing first
ID-sorted name match. Both forms redact bodies whose `disclosure_view` is not
`original` and return the view value; an original body remains visible to the
operator even when `promptable` is false. HTTP Asset read and publication
responses follow the same rule.
Slicing an Asset retains its source `disclosure_view` and `promptable` values,
so derived content does not silently become original or Worker-disclosable.
Copy full task, request and Asset IDs from the returned records. `--worker-id`
selects an execution identity for quick, pool and task/resume runs; use a
different ID from the producer. It does not grant verifier capabilities.
Configured fleets use their configured identities and reject this override.
Distinct signing identities do not prove distinct people or independent models.

The source must be a live independent-policy business task with an eligible
committed output and available allowance. Review reserves source allowance;
the default is one unit, not token billing or a fresh root grant. The reviewer
has no tools or delegation and sees only exact original promptable target,
rubric and evidence Assets. Same-name context is not silently added. Sealed,
redacted, non-promptable or tombstoned review context is refused.

A canonical retry of an eligible human or plan task can also request review when
it retains the independent policy and exact eligible output. The retry stays an
immutable retry task; its reviewer is ordinary human-origin work and is not
another retry. Method/plugin-labeled tasks and system, recovery or unknown origins
remain unsupported. A `changes` decision on a retry is recorded but leaves the
output unqualified; continuing that retry through `request-changes` is not
supported in this slice.

Reviewer completion creates an ordinary report and does not qualify the source.
Decision checks its exact ID, output name, immutable reviewer ancestry and
producer separation, then uses the existing policy-bound attestation owner.
The newly generated report is linked separately; it does not change the frozen
policy evidence. The operator's verdict is not automatically inferred from
report prose. Ordinary report completion retains same-name output matching; a
concurrent foreign output can complete that task, but it cannot pass decision
report checks or qualify source work.

Use `--verdict changes` to record rejected attestation and keep the output
unqualified. Decision itself does not publish a revision or reopen work. Use the
separate `request-changes` command below with the returned exact decision ID.
Ordinary sources cannot gain a review gate after execution. Already terminal or
qualified sources refuse new decisions; exact keyed replay still returns the
original durable request or decision. Changed input creates a new Candidate.
One request accepts at most one decision; request a new review for another
decision rather than overwrite its history.

The default decision actor is limited to `verify.human`. A different policy
requires an already-authorized actor/key and matching private key:

```bash
aig task review decide <request_id> --report <report_asset_id> --verdict accept \
  --reason "Evidence checked." --actor-id <actor_id> --key-id <key_id> \
  --key-file <private_key_file> --json
```

All three identity options must be supplied together. They do not authorize a
new actor or expand existing capabilities. Required permissions include
`task.review.decide`, `asset.attest` and the frozen policy's verifier capabilities.
History preserves attribution, exact policy/target/report links, original time
and revision; Redis event scopes include both source and reviewer tasks. Failed
or pending relay preserves accepted SQLite facts and reports their actual
acceptance state. See [ADR-031](../adr/ADR-031-policy-bound-task-review.md).

## Continue after a changes decision

```bash
aig task request-changes <decision_record_id> --reason "Correct the cited defect." \
  --idempotency-key corrected-report-1 --json
aig task revisions <source_contract_id> --json
aig run --task <successor_contract_id> --worker-id corrected-producer --json
aig task request-review <successor_contract_id> --output <output_name> \
  --asset <new_target_asset_id> --reason "Check the correction." --json
aig run --task <new_reviewer_contract_id> --worker-id corrected-reviewer --json
aig task review decide <new_request_record_id> --report <new_report_asset_id> \
  --verdict accept --reason "Exact content checked against the policy." --json
aig task result <successor_contract_id> --output <output_name> --to corrected.md --json
```

Copy the full successor ID returned by `request-changes`. Take exact target IDs
from each task's `run --json` output map; name-only Asset lookup can show an older
same-name attempt. Each new review returns its own reviewer ID, output slot and
request record; use those exact references rather than the previous report.
Inspect content before deciding, and record another changes verdict if needed.

Revision atomically cancels the old source and publishes a bounded child with
all still-unqualified output slots. Qualified slots remain history references;
the source stays cancelled even after successor acceptance. Original input roles,
trust checks, tools, routing/delegation and acceptance policy are retained. Inputs
and legacy label context freeze by exact ID; only the old target and report are
added as context. They do not widen frozen policy evidence. The new output needs
fresh qualification under the same policy. No claim reset or root grant occurs.

Omitted budget resolves the source's remaining allowance once; `--budget N` may
transfer less. Exact keyed replay returns the same link and resolved budget after
later execution. Other source outputs cannot silently disappear. Open holds,
unfinished descendants, exhausted allowance, existing terminals, unsupported origins
and protected minting are refused. Review itself may have left insufficient budget.
Publication does not execute a provider or stop the old OS process. History and
both task event scopes retain the exact revision link. See
[ADR-033](../adr/ADR-033-review-linked-task-revisions.md).

## Upgrade an existing workspace

Stop old Workers before using the new runtime with a schema17 workspace. Before
another writable command, run:

```bash
aig domain upgrade --json
aig redis rebuild --json
```

`upgrade` only opens an existing regular Store. It saves a SQLite backup under
`.aig/backups/` with private file permissions, then applies the existing migration.
Its response includes old/new schema versions, backup path and runtime revision.
If already current, it returns no-op with no backup. Missing or future schemas
fail before creating a backup or opening a writable Store; migration failure keeps
the completed backup available. Task facts, domain identity and keys are not reset.

Schema18 marks support for exact input binding. No table or column is added, and
old Contract IDs/facts remain unchanged. Older runtimes refuse to reopen the upgraded
database even if it has no v6 Contract. Read-only queries reject schema17 until it
is upgraded. Other writable Store opens migrate automatically without this command's
backup. Already-open old connections and active mixed-version Workers are outside
the reopen guarantee. Keep backups private. See
[ADR-032](../adr/ADR-032-exact-input-binding-reader-gates.md).

## Observe events and repair infrastructure

```bash
aig task events <contract_id> --after 0 --limit 1000 --json
aig task watch <contract_id> --after 0 --timeout 30 --json
aig redis status --json
aig redis sync --json
aig redis rebuild --json
aig --offline task audit <contract_id> --json
```

Omit the task ID to observe the workspace feed. Event cursors are SQLite record
revisions; task-scoped reads include descendant references. `next_revision`
advances across scanned events, including unrelated entries, so filtered consumers
can continue. Watch emits JSON lines and stops at its observation deadline; it
never dispatches work. A completed watch means observation ended normally, not
that a task succeeded. Missing tasks, task-wait timeout, failed task terminal,
and unavailable Redis return nonzero status.

`redis status` checks health and relay lag without repairing it. `sync` replays
pending immutable records and refreshes indexes. Query indexes bind their selected
Redis generation to the ordered SQLite record history, not revision equality
alone. `rebuild` is the explicit repair path and routes a fresh Store snapshot; a
non-ready generation is refused by reads until a successful rebuild. If a Redis
command fails during its Lua publication, earlier script commands are not rolled
back, so the generation stays fenced as non-ready and repair may need another
explicit rebuild. The command reports source conflicts and Redis publication
failures as nonzero bounded results. See [change 103](../../changes/103-query-source-binding-and-conditional-repair.md).
It repairs only the current domain's feed
and index; it does not flush other domains or alter SQLite facts. Workers use
Redis wake events with bounded waits and return to SQLite claims; external
publishers are discovered on the next relay pass.

An outage after SQLite commitment can report `redis_sync_pending` and retained
facts. Restore Redis and synchronize before interpreting or repeating the action.
CLI JSON includes `facts_retained` and `candidate_accepted` when known. The HTTP
surface exposes `/health/redis` and `/readiness/redis`; unhealthy infrastructure
returns 503. Selected `--offline` operations inspect snapshots for diagnosis;
execution and mutation never use this mode.

New board commands use `ok/data/revision` envelopes; legacy CLI fields remain
compatible. `watch` uses one object per line. The interface is inspired by
[Hermes Kanban's task commands](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md),
while task state and mutations continue to use Aigineering's signed fact runtime.
