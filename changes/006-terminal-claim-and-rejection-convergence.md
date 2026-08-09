# Change 006: Terminal claim and rejection convergence

Status: Implemented and verified
Target: v0.5.3

## Problem

A Contract can become terminal while an older worker claim is still active.
Without a transactional fence, that worker can publish a late Candidate after
the task was cancelled or completed. Separately, deterministic conflicts found
only during commitment can roll back correctly but escape without a durable
Candidate rejection.

Both failures violate the public runtime boundary: terminal work must not
accept new effects, and every rejected state-changing input must remain
observable.

## Intended change

- committing a terminal fact also closes any unrelated active claim for that
  Contract in the same SQLite transaction;
- the claim transition is an immutable fact, so rebuilding the claim index
  reproduces the same closed state;
- claim-bound commitment rejects a Contract that is already terminal;
- exact Candidate replay remains idempotent after claim closure;
- deterministic immutable-record conflicts become durable Candidate
  rejections after the conflicting transaction rolls back;
- storage or availability failures continue to surface as infrastructure
  errors rather than being mislabeled as worker rejection.

## Non-goals

- changing worker lease duration or routing policy;
- introducing process-local task state or locks above SQLite;
- changing ordinary Asset identity or the signed definition/content graph;
- treating arbitrary database failures as Candidate mistakes.

## Verification

- terminal and active-claim races have one accepted outcome and no late fact;
- claim rebuilding preserves terminal-driven release;
- exact committed-Candidate replay remains successful;
- conflicting effect batches leave no partial facts and do leave a durable
  rejection receipt and trace;
- full reconstruction, concurrency, lint, test, and build gates pass.

## Exit criteria

The change is complete when terminal state and claim fencing share one
transactional boundary, all deterministic commitment conflicts are observable,
and no compatibility path can reopen or silently bypass that boundary.

## Implementation evidence

Terminal commitment now releases an unrelated active claim atomically and
records the rebuildable transition. Claim acquisition and claim-bound commit
both fence terminal Contracts. Deterministic immutable-record conflicts become
durable Candidate rejections without misclassifying infrastructure failures.
Crash, reconstruction, concurrency, replay, and worker API tests cover the
shared boundary.
