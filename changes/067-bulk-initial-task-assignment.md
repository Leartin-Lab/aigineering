# Bulk initial task assignment

`aig task assign CONTRACTS... WORKER_ID --reason TEXT` assigns one registered
Worker to multiple exact Contracts through the existing signed assignment
publisher. The legacy single-Contract form and its output remain compatible.
Multi-target calls validate all IDs before Store or Redis access, derive stable
per-Contract replay keys from one returned batch key, and report independent
results in input order. A failed target does not roll back accepted siblings.

The application still enforces empty previous assignment and unassigned-only
semantics for each target. Exact replay retains the original Candidate and
assignment fact IDs. Accepted Redis synchronization pending results preserve
the exact target key and durable result; unexpected errors stop the batch and
leave uncalled targets explicitly not attempted. If a pending carrier says the
Candidate was accepted but omits its exact operation result, the command reports
an unknown outcome and stops instead of treating later targets as safely run.
This command does not reassign existing work or introduce a batch transaction
or scheduler.
