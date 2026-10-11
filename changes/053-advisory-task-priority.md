# Signed advisory task priority and shared claim ordering

Status: Development implementation; bounded integration verified.

Operators can publish an immutable priority hint with `task priority` and inspect
its full history with `task priorities`. A signed `task.priority.set` Candidate
emits one exclusively owned `task.priority.changed` fact. Integers from -100 to
100 are accepted; higher values are considered first. Zero is the default and
clear value, and clearing retains the history head. Every later update names
the exact previous fact. New terminal-task changes, no-ops and stale heads reject;
an exact authenticated replay returns its original fact.

One application service orders the supplied task IDs by descending priority,
original declaration revision and full ID. Broad Worker pulls and existing
scoped run, fleet and engine enumerations use that service. Runtime's generic
full-permutation hook preserves the original tasks and all eligibility checks;
an explicit task remains pinned. Priority does not transfer or reset claims,
override assignments, prerequisites or holds, or widen authority and allowance.

Task views expose the effective value and head. List and board offer explicit
priority sorting while retaining their default display order. Successors,
retries and children start without inherited priority. Publication and operational
reads require Redis; committed SQLite facts survive pending relay and rebuild.

No Contract identity, Store schema or reader floor changes. Ordering applies to
one scan snapshot, with no global queue, fairness, aging or start-time guarantee.
Older readers may ignore the hint. See [ADR-041](../docs/adr/ADR-041-advisory-task-priority.md)
and the [operator workflow](../docs/reference/task-board.md#prioritize-eligible-work).
Full regression, independently reviewed integration and installed-artifact
checks pass in [report 084](../reports/084-advisory-task-priority-2026-10-05.md).
Provider execution and stable qualification remain separate gates.
