# Change 009: One terminal fact construction owner

Status: Implemented and verified
Target: v0.5.3

## Problem

Asset reduction, independent acceptance, claim failure, Candidate cancellation,
and completion plugins legitimately derive terminal outcomes from different
causes. They also duplicate the low-level `lifecycle.terminal` payload and
record construction, allowing fields and validation to drift.

## Intended change

`core.lifecycle_facts` becomes the sole constructor and validator for terminal
RuntimeRecords. Derivation remains with the module that owns each cause, and
SQLite remains the single-assignment transactional arbiter.

## Non-goals

- routing all terminal causes through a scheduler or Plugin;
- changing terminal event names or payload compatibility;
- moving cancellation authority out of effect projection;
- weakening Store-level validation or uniqueness.

## Exit criteria

No production module constructs a `lifecycle.terminal` RuntimeRecord directly;
an architecture test enforces the owner, and terminal, recovery, acceptance,
claim, crash, replay, and reconstruction tests remain unchanged in behavior.

## Implementation evidence

- all seven production terminal derivation paths use
  `create_terminal_record()` from `core.lifecycle_facts`;
- optional actor, reason, causal parents, and recorded time remain canonical;
- Store validation and SQLite single-assignment arbitration are unchanged;
- an AST architecture test rejects any production direct terminal-record
  construction outside the lifecycle owner;
- focused terminal/claim/acceptance/recovery/crash tests and the full suite
  passed: 1128 tests, 3 skipped; Ruff check and format passed.
