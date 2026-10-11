# Aigineering

> **Smarter boundaries, not smarter models.**

Aigineering is a zero-trust runtime for AI workers. A Worker output is a
Candidate, not shared state. Only effects that pass signature, claim,
projection, authority, allowance, and acceptance checks become durable facts.

Aigineering provides harness-style entry points over a fact runtime. Models
may still produce incorrect content inside an authorized output; Aigineering prevents unauthorized output and invisible control flow
from becoming runtime truth.

## Quick start

Python 3.11 or newer and a running Redis service are required.

```bash
git clone https://github.com/Leartin-Lab/aigineering.git
cd aigineering
pip install -e ".[dev]"

# Required local collaboration service (or configure an existing Redis URL)
docker compose up -d redis
aig redis status --json

# Configure an OpenAI-compatible LLM Worker
export AIGINEERING_API_KEY="..."
export AIGINEERING_MODEL="your-model"
export AIGINEERING_BASE_URL="https://provider.example/v1"

# Run a generic OpenAI-compatible report task with an explicit source
aig run "build a report with citations" --input evidence=source.md \
  --output report.md --max-output-tokens 8192

# Inspect the resulting facts and trace
aig trace
aig asset ls
```

Initialize a signed local domain before publishing tasks or assets directly:

```bash
aig domain init
aig asset add --name source --content "reviewed evidence"
aig task create \
  --name research \
  --description "Produce a report from the disclosed source." \
  --input source \
  --activation source \
  --output report
```

Use `aig task list`, `board`, `stats`, `show`, `events` and `watch` to discover
and observe work; `task result ID --output NAME --to PATH` exports exact completed
output. `task cancel` and `task retry` publish signed actions. Redis supplies the
required event feed, read indexes and Worker wakeups; SQLite commits facts and
claims. See [task operations and Redis repair](docs/reference/task-board.md).

For multiple projects, `aig board create NAME` initializes an independent local
workspace; `board switch NAME` saves selection and `aig --board NAME ...` selects
one invocation. `board list/show/archive` manage local routing metadata. Explicit
`-C PATH` bypasses selection. Exact Genesis pins prevent silent routing to a
replaced domain; archive does not cancel work. See
[named workspace operations](docs/reference/task-board.md#named-local-workspaces).

Use `aig task runs ID --json` to find an exact claim, then
`aig task diagnostics ID --claim CLAIM_ID --json` for safe fact summaries and
stable cursor pages. Allocation alone does not prove invocation; submission
still requires the task's output acceptance. Missing or conflicting history
returns an integrity error. See [attempt inspection](docs/reference/task-board.md#inspect-claim-attempts).

Use `aig task context ID --json` to preview current input and method context as
metadata. Exact `--asset ASSET_ID --include-content` explicitly reads one
disclosed body with redaction and content guards preserved. This preview does
not establish readiness or historical claim context. See
[context inspection](docs/reference/task-board.md#inspect-task-context) and its
[bounded verification](reports/092-task-context-view-2026-10-07.md).

Local quick, pool, target and configured/Fleet execution also capture typed
invocation and provider retry events. Use `aig task logs ID --claim CLAIM_ID
--json`, adding `--store PATH` for a custom database or `--follow --timeout 60`
for a bounded live reader. Logs omit prompts, output bodies and credentials;
invocation finish does not establish submission or accepted output. See
[local operational logs](docs/reference/task-board.md#inspect-local-worker-logs)
and [bounded verification](reports/093-local-worker-operational-logs-2026-10-07.md).

Use `aig artifact import FILE --name NAME --media-type TYPE` to publish a
bounded file, then `aig task attachment add TASK_ID ASSET_ID --json` to associate
it with a task. `list` shows safe history and `export ... --output PATH` copies
verified original bytes to a fresh path. Associations preserve task inputs and
acceptance requirements. See [task attachments](docs/reference/task-attachments.md)
and [bounded verification](reports/095-signed-task-attachment-links-2026-10-07.md).

Assignment and priority mutations retain exact recovery keys and committed IDs
when Redis relay is pending; see
[the recovery contract](changes/066-assignment-priority-durable-pending-results.md)
and [verification](reports/096-assignment-priority-pending-results-2026-10-07.md).

Local integrity comparison also checks Worker registration actor/key bindings;
see the [verification correction](reports/094-registration-identity-materialization-digest-2026-10-07.md).

Bulk block/unblock/cancel and complete per-task hold-map promotion replay use
those same signed owners, with ordered partial results and exact replay keys.
Their [development verification](reports/087-bulk-task-lifecycle-2026-10-07.md)
includes a packaged CLI control against real Redis. Authentication failures now
retain only rejection audit before claim/lifecycle processing; see
[the bounded repair evidence](reports/088-authentication-rejection-isolation-2026-10-07.md).
The wider CLI roadmap and stable qualification remain open.

Mock execution is an explicit deterministic dry-run, never the production
default:

```bash
aig demo "build a report with citations" --worker mock
```

Each quick-goal invocation declares its own output name and runs claims within
that root task's descendant tree. Assets intentionally shared by exact name in
ordinary tasks remain reusable across the local domain. With `--json`, use the
returned `contract_id` to read the root's `outputs`; the session manifest's
`produced_asset_ids` identifies Assets committed by that run.

Quick-goal `aig run GOAL` uses the goal and only the files explicitly supplied
with repeatable `--input NAME=PATH` options. It does not scan the workspace,
fetch network sources, or grant tools. Input files must be non-empty UTF-8 and
at most 1 MiB each. A successful exportable text output is saved as Markdown;
`--output PATH` chooses the destination, and an existing path is left intact.
`aig demo` remains the path for built-in examples and explicit mock dry-runs.
Without `--config`, choose exactly one of GOAL, `--once`, or
`--task`/`--resume`. `--input` and `--output` are
GOAL options; quick-goal mode rejects a tool registry and mock-output
overrides. `--behavior-label` and `--save-config` also require GOAL.

For a configured DeepSeek-compatible endpoint, keep the key in an environment
variable and map it to Aigineering's key variable:

```bash
export AIGINEERING_API_KEY="$DEEPSEEK_API_KEY"
export AIGINEERING_MODEL="deepseek-flash"
export AIGINEERING_BASE_URL="https://api.deepseek.com"
aig run "基于 README 中的实现说明，生成中文能力、操作步骤和局限报告。" \
  --input readme=README.md \
  --output report.md \
  --thinking-mode disabled \
  --max-output-tokens 8192 \
  --response-format json_object
```

`--json` controls Aigineering's CLI output. `--response-format json_object`
selects the provider's JSON wire mode; the adapter still compiles only the
canonical action wrapper and rejects malformed or unknown fields.

For an existing Contract, `aig run --task ID` targets that Contract and its
immutable descendants. `--once` runs one local task-pool cycle; it is not a
promise of exactly one provider request. Both modes run domain-wide maintenance.
Target runs report projected status, blockers, stop reason, and suggested next
step. Recovery successors remain explicit work and must be selected by their
actual IDs. See [run cycles and retries](docs/reference/task-board.md#run-cycles-and-retry-boundaries).
An authorized output is a committed assertion, not independent proof of semantic
truth. `--wait-timeout`, `--interval`, and provider `--timeout` must be finite
positive numbers.

Complete declarative examples also use `aig run`:

```bash
mkdir -p workspaces/report
export DEEPSEEK_API_KEY="..."
aig -C workspaces/report run --config /absolute/path/to/examples/report-consistency/run.toml \
  --wait-timeout 300 --json
# Continue the actual returned task ID in the same workspace:
aig -C workspaces/report run --config /absolute/path/to/examples/report-consistency/run.toml \
  --resume TASK_ID --wait-timeout 300 --json
```

Configuration explicitly selects files, Skills, a root task and Worker profiles.
The old `fleet run` entry delegates to the same application service. New starts
refuse historical output slots; use a fresh workspace for an independent run.
See [configured runs](docs/reference/configured-runs.md) for path conventions,
strict validation, continuation and the shared entry's limits.

Existing agent harnesses can keep their own model and tool orchestration while
using signed pull/submit Candidates. See the
[agent harness migration reference](docs/reference/agent-harness-migration.md).

Executable AI-for-science integrations are available as installable example
skills: [`literature-evidence`](examples/literature-evidence/SKILL.md) turns
retrieval through independent review into separately testable tasks, while
[`scientific-data-profile`](examples/scientific-data-profile/SKILL.md) safely
profiles authorized CSV/TSV inputs without disclosing raw rows. Both use
zero-dependency adapters and offline replay fixtures. The
[`runtime-compiled AI4S run`](examples/ai4s/README.md) additionally exercises a
Skill-guided root task, a capability-routed local Fleet, and ordinary tool
continuations. The runtime-only acceptance path also covers staged planning,
independent `/attest`, root qualification, and SQLite reopen without relying
on the example audit driver.

Declarative non-tool case studies are indexed in
[`examples/README.md`](examples/README.md), including report consistency and
claim-evidence review. Their scenario evidence is collected in
[`reports/063-runtime-native-cases-2026-09-13.md`](reports/063-runtime-native-cases-2026-09-13.md).

## Current local preparation

The current source is **v0.5.13.dev0**, adding the configured-run entry, task
operations and required Redis collaboration. Verification is recorded in
[report 071](reports/071-unified-run-entry-2026-10-03.md) and
[report 072](reports/072-task-cli-required-redis-2026-10-03.md). A bounded real
repository-report execution and its content-review defects are recorded in
[report 073](reports/073-required-redis-live-report-2026-10-04.md). Stable-release
qualification and tagging remain a separate closure.

Signed comments, pre-claim holds and active leaf-task handoff have development
verification. Handoff atomically cancels the old attempt and transfers bounded
unfinished work to a successor; it preserves frozen scope, policy and audit
history. See the [task operations guide](docs/reference/task-board.md) and
[handoff evidence](reports/076-signed-task-handoff-2026-10-04.md).
Policy-bound review now publishes an ordinary bounded reviewer task and records
an exact report decision through the existing acceptance owner. Use distinct
`run --worker-id` execution identities and the [review workflow](docs/reference/task-board.md#request-an-exact-independent-review).
The [review evidence](reports/077-policy-bound-task-review-2026-10-04.md) includes
actual DeepSeek producer/reviewer execution and a retained changes decision;
the report did not pass independent acceptance. Shared completion now fences
unfinished descendants at every level. Historical already-completed trees still
need authorized repair.
An exact changes decision can now publish a bounded revision successor with
`task request-changes`. Original input roles and trust are retained with exact
input bindings; later same-name Assets cannot widen revision context. Continue
the returned ID, then review and qualify its new output. The original stays
cancelled. [Revision evidence](reports/078-review-linked-task-revisions-2026-10-04.md)
retains the failed attempts and records fresh qualification and export. The root
agent signed that accept decision under user authorization; it does not establish
independent natural-person review. Existing workspaces can use `domain upgrade` to
back up before the current schema21 migration; stop old Workers first.
[Expanded handoff](docs/adr/ADR-035-expanded-root-handoff.md) now closes selected
old descendants and returns unused allowance to one ordinary successor, including
when the source itself has no available balance. It preserves outside independent
review work and explicitly repairs missing historical completion terminals during
authorized handoff. [Expanded-handoff evidence](reports/079-expanded-root-handoff-2026-10-04.md)
separates deterministic checks, real execution and content limits. The broader
Kanban CLI goal remains open; reviewer completion
alone does not accept source work.

Submitted work that awaits independent acceptance now exposes exact remaining
output Assets in `qualification_pending_outputs`. Use the task's review and
revision workflow to qualify or correct them. Domain-wide signed claims also
retain inspectable attempt provenance. See the
[bounded repair evidence](reports/090-submitted-task-diagnostics-2026-10-07.md).
The subsequent installed-runtime report completed ordinary review, bounded
revision, policy acceptance and exact export; see
[real workflow evidence](reports/091-real-repository-report-acceptance-2026-10-07.md)
and the unchanged [Chinese report](REPOSITORY-REPORT.zh-CN.md).

Task ordering now has signed `task link` / `task unlink` mutations and a
read-only `task dependencies` query. An exact prerequisite must reach typed successful completion before a
new claim; failed or cancelled prerequisites stay visible blockers. Links do not
change frozen input Assets or output acceptance. See the
[dependency workflow](docs/reference/task-board.md#order-work-with-exact-prerequisites)
and [design change](changes/048-task-execution-dependencies.md). Bounded
[development verification](reports/080-task-execution-dependencies-2026-10-04.md)
includes real provider work; broader Kanban and stable-release gates remain open.

Local and HTTP Worker claims and renewals now share signed commands and
transactional authentication. `worker next --candidate JSON` accepts a registered
harness's signed request; ordinary local pulls use their existing authorized
Worker signer. Explicit flags cannot change signed values. See
[the coordination change](changes/049-authenticated-worker-coordination.md) and
[the harness guide](docs/reference/agent-harness-migration.md).
[Development verification](reports/081-authenticated-worker-coordination-2026-10-05.md)
separates full tests and installed fixtures from real provider evidence.

`worker registrations WORKER_ID` exposes current registration and immutable
history. Local pull-only setup can transition once to a concrete adapter under
the same Worker ID and key through a signed profile promotion, with active-claim
and writer-version checks. Setup verification is separate from actual model
output; [the entry change](changes/051-local-worker-profile-promotion.md)
records the current verification scope.

Worker selection now has signed `task assign` / `task reassign` mutations and
`task assignments` history. Claims enforce the selected Worker transactionally;
claimed tasks require a successor instead of in-place reassignment. Handoff and
revision inherit assignment, with an explicitly authorized override when needed.
See [assigning exact work](docs/reference/task-board.md#assign-exact-work) and
[the development change](changes/050-signed-task-assignments.md). Deterministic
and installed [development verification](reports/082-task-assignments-2026-10-05.md)
is separate from the later bounded
[real assigned-Worker report](reports/091-real-repository-report-acceptance-2026-10-07.md).
That case verifies inherited producer assignment and distinct reviewer execution;
wider assignment and stable qualification remain separate.

Initial `task assign CONTRACTS... WORKER_ID --reason TEXT` also supports up to
64 tasks for the same Worker. Each target retains its signed assignment owner,
and ordered partial results include actual replay keys. Single-target usage is
unchanged; [the bulk assignment change](changes/067-bulk-initial-task-assignment.md)
describes recovery and the boundary with remaining bulk operations.
Current [verification](reports/097-bulk-initial-task-assignment-2026-10-07.md)
separates source controls from installed and provider evidence.

For routine reassignment, use `aig task reassign CONTRACTS... --current
--worker WORKER_ID --reason TEXT` (or `--clear`). The command reads all original
heads together and saves the request before attempting any target. It prints a
copyable `aig -C WORKSPACE task reassign --resume REQUEST_ID` command; recovery
uses the saved heads and intent, so newer assignments cannot be silently
substituted. The request is local input context, while signed Candidates and
SQLite facts remain authoritative. See [request recovery](changes/069-reassignment-request-recovery.md)
and [verification scope](reports/099-reassignment-request-recovery-2026-10-08.md).

The explicit `task reassign CONTRACTS...` form accepts a common replacement Worker or a common
clear action, with an exact `--previous-for CONTRACT=HEAD` for every member.
It returns ordered partial results and keys bound to each target and original
head. Single-target `--previous` remains supported. See
[the bulk reassignment change](changes/068-bulk-task-reassignment.md);
[verification and limits](reports/098-bulk-task-reassignment-2026-10-08.md)
distinguish source, installed and provider evidence.

`task priority` publishes a signed advisory hint, and `task priorities` exposes
its exact history and copyable head. Broad and scoped application scans share
one ordering service; list and board offer `--sort priority`. Holds, dependencies,
assignment and claim fences still decide eligibility. See
[prioritizing eligible work](docs/reference/task-board.md#prioritize-eligible-work)
and [the development change](changes/053-advisory-task-priority.md). Integrated
[development verification](reports/084-advisory-task-priority-2026-10-05.md)
passes; concurrent workers have no global order or fairness guarantee. The wider
Kanban goal and stable qualification remain open.

`task promote ID... --reason TEXT` atomically releases each task's complete
observed manual hold set. `task promotions ID` shows its audit history; results
include the exact replay key and remaining blockers. Multiple tasks can partially
succeed, and other eligibility fences remain effective. See
[manual recovery](docs/reference/task-board.md#recover-manually-held-work) and
[the development change](changes/054-exact-hold-set-promotion.md). Bounded
[development verification](reports/085-exact-hold-promotion-2026-10-05.md) passes;
the wider Kanban and stable gates remain open.

`task edit ID --title TEXT --body TEXT --reason TEXT` replaces one or both fields
through an immutable handoff successor. Omitted text stays unchanged, empty body
is allowed, and the returned new ID retains bounded scope, assignment and copied
source holds. `task edits ID` reads the shared history. See
[editing instructions](docs/reference/task-board.md#edit-task-instructions) and
[the development change](changes/055-text-edit-through-shared-handoff.md).
[Integrated development checks](reports/086-task-edit-successors-2026-10-06.md)
pass, including independent document and real-Redis control review; the wider
Kanban and stable gates remain open.

The v0.5.12 source snapshot completed deterministic, package, installed-artifact,
chain, and nested-replanning verification. This is locally verified release
preparation, not a published or tagged release. See the
[CLI stabilization evidence](reports/070-usable-cli-stabilization-2026-10-03.md).

v0.5.11 added declarative method packages and a local create → test → independently
accept → reuse lifecycle. Methods run as ordinary Contracts; generated methods
remain unverified until explicitly evaluated. See the [method guide](docs/methods.md)
and [fixture example](examples/method-governance/README.md).

The v0.5.10 baseline converges Contract construction and stateless runtime
maintenance, fixes concurrent trace reconstruction and local signing-key
publication, and preserves the v0.5.9 protocol and SQLite schema. The artifact
adapter provides bounded attachments, extraction, exact evidence and offline
citation export; see the [artifact guide](docs/business-artifacts.md).

The current quick-goal and target-task CLI changes are documented in
[`changes/033`](changes/033-usable-goal-cli.md),
[`changes/034`](changes/034-target-task-cli.md), and
[`changes/035`](changes/035-provider-output-diagnostics.md), and
[`changes/036`](changes/036-provider-json-action-wire.md). Their integrated
release evidence and retained failure cases are in
[report 070](reports/070-usable-cli-stabilization-2026-10-03.md).

## Reference runtime scope

The single-machine reference runtime provides:

- actor-signed Candidate publication;
- one Candidate commitment boundary for CLI, Worker, Plugin, and HTTP surfaces;
- SQLite transactions for claims, fencing, projection, facts, trace, and
  idempotency;
- reconstructable runtime views derived from append-only records;
- claim-bound Worker packages and authenticated submissions;
- staged planning and replanning as ordinary independently claimable tasks;
- causal allowance containment for recursive publication;
- independent output attestation where a Contract requires it;
- deterministic replay, lineage, audit, and task-status views;
- same-machine active-active Worker arbitration over one SQLite domain;
- mock and OpenAI-compatible LLM Workers;
- optional FastAPI integration through the `api` extra;
- required Redis collaboration for indexed discovery, replayable events and
  Worker wakeups, reconstructable from SQLite;
- separate content, signed-definition, and signed-association identities with
  many-to-many history;
- v4 Contracts that bind label-selected context to exact Asset IDs;
- v5 Contracts that separate execution requirements from delegated routing
  scope;
- graph-native ordinary Worker outputs signed by the WorkerHost;
- terminal/claim fencing and durable deterministic commitment rejections;
- one causal allowance source and one terminal-fact constructor;
- planning label containment before Candidate commitment;
- explicitly configured, Contract-scoped tool registries and a separate
  ToolWorker;
- exact verifiable slice derivations and claim-gated disclosure;
- independently attested descendant outputs;
- deterministic JSON output shapes that independent review cannot override;
- executable AI4S literature and safe data-profile examples;
- heterogeneous local Worker fleets using independent SQLite connections;
- parallel tool calls compiled into ordinary tasks and a boolean join;
- durable recovery from claim-bound structural output rejection;
- executable local tool contracts with deterministic input/output schemas,
  version binding, UTF-8 output limits, and descriptor-drift rejection;
- structured tool execution metadata and a read-only `task audit --json`
  productivity projection derived from durable lineage facts;
- a runtime-only AI4S/Fleet tool-observation → continuation → independent
  verifier loop that survives SQLite reopen.

The release has deterministic boundary, reconstruction, concurrency, artifact,
and bounded real-LLM evidence. See the
[v0.5.3 convergence report](reports/053-boundary-convergence-2026-08-09.md) and
the [v0.5.4 AI4S evidence](reports/054-ai4s-auditable-example-2026-08-13.md),
the [v0.5.5 Fleet evidence](reports/055-local-worker-fleet-2026-08-14.md), and
the [v0.5.6 tool-closure evidence](reports/056-tool-closed-loop-productivity-2026-08-23.md).

Inspect the accepted asset graph without changing authoritative state:

```bash
aig graph contents --json
aig graph definitions --json
aig graph assertions --json
```

The graph is reconstructed from SQLite RuntimeRecords. Similarity adapters may
propose signed relations, but a score is neither identity nor authority.

Normal CLI and API operations require Redis; its Python client is a base
dependency. Start the local service or select an existing deployment:

```bash
docker compose up -d redis
export AIGINEERING_REDIS_URL="redis://127.0.0.1:6379/0"
aig redis status --json
aig redis rebuild --json
```

SQLite remains authoritative for Candidates, facts, claims, allowance,
acceptance and terminal state. Operational Redis failures return visible errors;
selected `--offline` audit commands remain available. Legacy `cache` commands
delegate to Redis maintenance. See [task operations](docs/reference/task-board.md)
and [ADR-027](docs/adr/ADR-027-required-redis-collaboration.md). Historical evidence
for the earlier optional adapter is recorded in
[`reports/051-redis-query-projection-2026-07-31.md`](reports/051-redis-query-projection-2026-07-31.md).
The signed graph acceptance evidence is recorded in
[`reports/052-signed-definition-content-graph-2026-07-31.md`](reports/052-signed-definition-content-graph-2026-07-31.md).

v0.5.7 adds fail-closed tool argument and schema fixes plus historical ADR
clarifications. See [patch evidence](reports/057-tool-validation-patches-2026-09-06.md).

v0.5.8 adds backup-first reconstruction diagnostics, reproducible scaling
measurements, AST dependency checks, and broader automated release gates.
See the [release evidence guide](docs/reference/release-evidence.md) and
[v0.5.8 evidence](reports/058-reproducible-release-evidence-2026-09-06.md).

## Non-goals

The current runtime does not claim:

- cross-machine consensus or distributed Store semantics;
- public-network deployment hardening;
- an external security audit;
- semantic truth of model-produced content;
- scheduler fairness across independent machines;
- a generic workflow language;
- production MCP transport;
- process-level tool timeout, cancellation, or isolation;
- exactly-once delivery for external side effects;
- cross-machine Store and Worker discovery.

The optional HTTP adapter binds to the local reference runtime. Authentication,
TLS, rate limiting, and hostile-network controls remain deployment
responsibilities.

## Core boundary

```text
Contract + disclosed facts
        ↓
authenticated Worker claim
        ↓
signed Candidate effects
        ↓
projection + authority + allowance + acceptance
        ↓
atomic fact and trace commitment
```

The non-negotiable rules are documented in
[`docs/boundary-invariants.md`](docs/boundary-invariants.md). The implemented
architecture is described by [`DESIGN.md`](DESIGN.md).

## Development

Development happens on `dev`. Stable releases are promoted to `main` after the
full CI and artifact gates pass. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT — see [`LICENSE`](LICENSE).
