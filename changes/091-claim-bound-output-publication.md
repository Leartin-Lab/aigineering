# Change 091: Require claim-bound output publication provenance

Independent attestation, task review, and submitted-output views now require an
exact immutable output publication from a declared producer Contract and a
Candidate head paired with the matching `claim.submitted` and `claim.granted`
facts. Graph `source_uri` metadata no longer establishes task-output ancestry
by itself. Existing Asset identity, graph signer, and report/attester separation
rules remain unchanged; the outer publishing Worker is not required to be the
graph assertion signer.

The proof is shared by canonical acceptance and its application/plugin read
paths. It adds no protocol fields or persisted tables.

No migration changes existing materialized facts, and historical records are
not rewritten. Stop older Worker and committer processes before relying on the
new qualification rule; already-open older processes are not fenced by a reader
floor. The normal single-task, single-round claim and submission flow is
unchanged.
