# Verified read-only task lineage

`aig task lineage CONTRACT_ID [--json]` navigates the selected Contract's
connected component using exact published parent containment and recovery
publications whose source has a canonically proven Worker invocation failure.
The component is bidirectional: starting at either endpoint of a verified edge
returns the same component. CLI help names the covered relation families and
states that other recovery families remain outside this view.
The reader verifies declaration, receipt, committed head and member identity
against the materialized Contracts in one immutable task-view query context.
Each task retains its existing status, blockers, assignment and output view;
typed edges carry safe evidence IDs. Invocation failures and schedule facts are
shown as source diagnostics and are not paired one-to-one with recovery
publications. A schedule is labeled verified only when it matches the exact
source failure proof; malformed or unmatched schedule records remain explicitly
unverified observations. Source grant, failure and release evidence must predate
the recovery publication receipt, while its typed schedule corroboration may be
recorded later by ordinary recovery processing. Human output includes the
receipt, declaration and head IDs for each edge and distinguishes verified
schedules from unverified schedule observations.

The command is read-only and requires the normal Redis collaboration service.
It does not create group status, alter readiness or acceptance, reset claims,
schedule recovery, or repair history. Recovery sources other than Worker
invocation failures are not yet included as verified publication relations.
Malformed or conflicting evidence is reported as a safe issue and cannot expand
the verified component.

Bounded development verification covers both endpoints, malformed history,
publication ordering, required Redis failures, read-only inspection and
private-copy reconstruction. See the [verification report](../reports/101-verified-task-lineage-2026-10-10.md)
for the tested snapshot and remaining lifecycle gates.
