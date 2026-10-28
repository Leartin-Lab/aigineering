# Bulk task reassignment and clear

`aig task reassign CONTRACTS...` supports replacing one Worker assignment
across several exact Contracts or clearing those assignments in a separate
homogeneous batch. Each target requires its own exact, nonempty
`--previous-for CONTRACT=ASSIGNMENT_HEAD` mapping. The existing single-target
`--previous` form and its success output remain compatible. The assignment
owner still validates and signs each target independently; a stale head,
claim history, or other existing fence remains effective.

Batch results preserve input order and include each validated requested head
as input context, the actual per-target key, and exact Candidate/fact IDs when
available. The requested head is not proof of the stored head or authority.
Per-target keys bind the batch base, Contract, and exact predecessor, so order
and sibling subsets do not change a member key. Use a returned actual target
key with the single-target command to replay one member.

Targets remain independently atomic. Known target rejections are reported and
collection continues. Exact accepted results with Redis synchronization
pending remain accepted and collection continues through the existing
per-target pre-sync; targets reached while Redis remains unavailable are
reported not attempted. Accepted pending without exact result data and other
unknown exceptions stop the batch, preserve earlier results, and mark later
targets not attempted. This adds no batch transaction, queue, claim reset, or
scheduler.
