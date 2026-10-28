# Refresh Redis sync captures after watched-key conflicts

Status: Implemented development increment
Date: 2026-10-23

When `sync()` loses a Redis transaction because a watched collaboration key
changed, it now captures the same configured Store again if attempts remain. It
uses that capture only when its revision advanced and its complete earlier
prefix matches the original capture. A non-advancing capture leaves the
original snapshot in place; a changed prefix stops with `source_diverged`.

The existing bounded attempt count is unchanged. The final failed attempt does
not recapture, and repeated Watch conflicts still end as `sync_contention`.
Non-Watch Redis errors and `events()`'s frozen capture behavior are unchanged.

Tests cover two signed Asset publications relayed by a second Store connection
while the first sync is inside its watched transaction, plus unchanged-source,
forked-prefix, and persistent-conflict cases. A continuously changing Redis
state can still exhaust the fixed retry budget.
