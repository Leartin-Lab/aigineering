# ADR-044: Bounded local Worker operational logs

Status: Accepted for the bounded implementation slice
Date: 2026-10-07

## Context

Task runs and diagnostics derive claim history from durable facts. They cannot
show in-progress local adapter retries, while raw traces and provider response
bodies are not suitable as a general operational log. Operators need a way to
inspect safe, task-scoped local invocation events without creating another
task-state or completion owner.

## Alternatives considered

Rebuilding logs from Runtime facts was rejected because facts record accepted
protocol transitions, not local retry attempts or invocation-time events.
Capturing Python logger output or stdout/stderr was rejected because those
streams do not carry a reliable exact-claim binding and can include prompts,
credentials, bodies or arbitrary Worker text. Persisting logs as SQLite facts
was rejected because it would add a new writer, retention and projection owner
to data that is auxiliary process observation rather than signed task state.

## Decision

Capture a finite set of typed events at the existing application invocation
boundary and the built-in LLM retry branches. Use one invocation-scoped observer
and store captures beside the selected SQLite database in .aig/worker-logs.
The quick, pool, target and configured/Fleet local paths enable capture by
default. The task logs command reads the matching capture; the store option
selects a custom Store root. Follow is locally bounded and Ctrl-C stops only
the reader.

Before exposing an event, reuse the task diagnostics exact grant/receipt proof
for the selected claim. The file header must match the proven domain, Contract,
claim, epoch, package and Worker. Any selected task-scope integrity issue blocks
association. The reader does not copy claim-proof logic or infer a capture from
facts.

The only event fields are fixed timestamps, sequence, elapsed duration, bounded
retry backoff, numeric provider status, retry counters and fixed failure
categories.
Never capture stdout/stderr, prompts, keys, headers, URLs, response bodies,
raw Candidate output, tool arguments/results, Asset content, arbitrary
exceptions or custom logger messages. A local hash chain detects accidental
reordering or corruption but does not authenticate the file.

Captures are exclusive-create auxiliary files with private directory/file
modes, per-claim and workspace caps, and nonblocking quota-lock acquisition.
They are never resumed or pruned automatically. File errors are best effort and
cannot change signed execution. Run and Fleet outputs report capture warnings
separately from Worker errors. An absent file has unknown cause; a persisted
prefix without invocation-finished is incomplete. `complete` means only that
the captured invocation call ended; it does not prove Candidate signing,
submission, acceptance or task completion.
The read command still requires Redis; SQLite remains the signed fact and claim
authority.

No Store table, schema change, scheduler, task status, business output,
acceptance effect or protocol event is introduced. Engine inner invocations
mask an outer observer; custom Worker-created threads, remote HTTP, arbitrary
stdout/stderr, full transcripts, retention and remote log sources are outside
this slice. A successful capture says only which allowlisted local events were
persisted; it does not prove provider quality or independent acceptance.

## Consequences

The capture owner is an application diagnostic service and stays separate from
the task lifecycle and Candidate commitment. Cursor checkpoints bind the
normalized Store root, domain, Contract, claim, capture, complete-line byte
offset, sequence and prefix hash. Readers reject a changed prefix and only
expose complete validated records. Follow EOF is not a task terminal and
consumer delivery is not exactly once.

Per-claim storage is bounded to 256 KiB and 512 events; a workspace spool is
bounded to 64 MiB. The quota lock is nonblocking; synchronous local filesystem
operations, including open, write and fsync, may still add latency, and no
filesystem liveness guarantee is claimed. Unsupported locking or storage
failures disable capture without changing execution.

Deterministic tests and fake-transport retry controls verify the selected
behavior. They are not real-provider evidence or stable-release qualification.
