# Change 096: Reconcile accepted recovery publications after interruption

Recovery replay now recognizes previously accepted Contract and context Asset
publication only from the exact signed Candidate intent, receipt, shared
committed-head/member proof, canonical projections, and matching materialized
objects. This lets an existing accepted recovery be recorded as scheduled after
a crash, including after its signing key is revoked, without signing or
authenticating a new Candidate. A same-ID Contract without that publication
history remains a conflict; new publication still uses current authentication.

The publication and recovery outcome are still separate writes. This change
does not provide an atomic transaction between them or re-evaluate historical
allowance availability.

Validation: focused signed WorkerExecutionError crash/reopen and revoked-key
replay checks, post-publication parent-allowance change, exact replay and
same-ID conflict checks, architecture gate, Ruff, format, and diff checks.
