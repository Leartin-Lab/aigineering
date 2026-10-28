# Local Worker identity across entry points

Status: Implemented and verified as a bounded development slice; not a stable release.

The shared local registration service supports an empty-domain pull-only Worker
followed by a concrete local adapter under the same Worker ID and signing key.
It publishes a narrow signed profile promotion and a new immutable registration
version atomically. The original registration remains unchanged. Unsupported
metadata or identity changes require explicit operator registration; no private
identity is replaced. Active claims block this automatic transition.

Local hosts retain their resolved registration version through claim selection
and the SQLite writer fence. A host created before a version change is visibly
refused. Repeated default adapter launches reuse a proven completed promotion
without adding facts. `worker registrations WORKER_ID` provides a required-Redis
operational read of current metadata, historical facts and promotion records.
A no-work `worker next --json` still returns `null`.

The plugin owns its audit fact and uses the existing generic transactional
revalidation seam. Built-in registration facts, ordinary explicit registration,
external signed coordination payloads and schema21 remain unchanged. Profile
is descriptive metadata. Stop older local Worker processes before promotion;
mixed-version adapter matching is outside this guarantee.

See [ADR-039](../docs/adr/ADR-039-local-worker-profile-promotion.md) and the
[operator guide](../docs/reference/task-board.md#inspect-local-worker-registration).
Model-adapter setup and signed fixtures do not establish real-provider output
or stable-release qualification.
