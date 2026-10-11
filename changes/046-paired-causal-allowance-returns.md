# Paired causal allowance returns

Unused descendant allowance can fund continued work without issuing another
root grant. A return debits its live donor and credits an original funding
ancestor by the same amount. Canonical grant/reservation paths, Candidate
receipt/head and same-batch donor cancellation bind that conservation decision.
Projection validates returns before successor reservation; SQLite repeats the
checks against current facts. Exact replay cannot credit twice.

SQLiteStore and MemoryStore raw record append reject all balance-changing
allowance facts. New ingress grants and reservations must match exact declared
budgets, deterministic funding identities and committed declaration provenance.
SQLite schema19 prevents normal reopen by credit-only schema18 readers. Upgrade
preserves valid history, refuses unsupported old return rows or invalid historical
funding facts, and leaves a failed migration's rows and version intact. Stop old
Workers and use the backup-before-upgrade flow; already-open old connections are
outside the reader gate.

The generic owner contains no handoff feature semantics. Its sole current return
producer is the signed handoff plugin, with existing cancellation/publication
authority and writer-lock reprojection. Store implementation code remains trusted;
these checks do not authenticate arbitrary local interpreter access or coordinated
database rewrites. Allowance remains a work-expansion limit, not token billing.

See [ADR-034](../docs/adr/ADR-034-paired-causal-allowance-returns.md). This development
change does not establish full Kanban parity or stable-release qualification.

Development evidence is in [report 079](../reports/079-expanded-root-handoff-2026-10-04.md).
