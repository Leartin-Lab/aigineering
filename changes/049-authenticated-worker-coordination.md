# Authenticated Worker coordination

Status: Implemented in the 2026-10-05 development tree; independent integration review remains separate.

## Problem

Operational claims and lease renewals needed the same actor authentication and
transaction-time identity fencing already used for Worker submissions. Applying
a signed check only in HTTP or before Store entry left local execution paths
and registration-change races with weaker guarantees.

## Result

HTTP, local WorkerHost, Fleet, Engine, method, CLI, and harness paths carry one
strictly parsed signed Candidate into the SQLite writer. The writer rechecks the
current actor key, `worker.submit`, enabled registration and claim binding
inside the claim/renewal transaction. Shared WorkerHost and harness helpers
compile coordination Candidates. Successful requests are single-use; a
null-target scan that finds no work commits no request and may be retried.
Claims also fence the registration version selected during package preparation;
renewals recheck the current enabled registration and actor/key binding without
adding a separate version field to their signed payload.

Signed claim grants link to their canonical claim request. Signed renewals link
to both the original grant and renewal request, whose provenance reaches the
Candidate receipt. Trusted recordless Store compatibility keeps its legacy
fact shape and is not considered an operational authenticated path.

`aig worker next --candidate` accepts inline signed Candidate JSON for an
externally registered identity. The CLI does not infer or provision a foreign
Worker's key. Expected auth and writer-fence errors use structured JSON or
Click diagnostics. Schema20 and the existing fact types remain unchanged.

See [ADR-037](../docs/adr/ADR-037-transactional-worker-authentication.md) for
the decision and [ADR-012](../docs/adr/ADR-012-authenticated-worker-coordination.md)
for its original HTTP scope and supersession note.

This change record does not claim a stable release or completion of the wider
Kanban CLI roadmap. Verification claims must be taken from the exact development
snapshot's focused and integrated evidence.
