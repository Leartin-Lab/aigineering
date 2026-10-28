# Reuse task-view projection snapshots for descendant risks

Status: Implemented development increment
Date: 2026-10-23

Aggregate task status now passes its query's frozen effect projection context
through descendant failure-risk analysis. A submitted child with independent
outputs is checked against the same captured fact snapshot as the parent view,
so aggregate reads do not fall back to loading projection facts from the narrow
read facade. Direct Store calls retain their existing projection-context
fallback.

This change adds no Store reads, cache, or facts to the read facade. It keeps
task views as read-only projections over the query's captured data.
