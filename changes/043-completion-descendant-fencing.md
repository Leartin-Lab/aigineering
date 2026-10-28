# Fence unfinished descendants when their obligation completes

Date: 2026-10-04
Source version: 0.5.13.dev0; development only, no release tag
Status: Completed development fix; historical repair remains open

Previously, completion cancelled unfinished direct children but could leave
grandchildren claimable. The shared pure FactReducer now traverses the full
descendant tree through satisfied and already-terminal intermediates. Existing
terminal facts remain unchanged; branches still serving a live outer independent
policy remain protected. Duplicate cancellation consequences in one Asset batch
are deduplicated.

Ordinary completion and independent qualification retain the same materialization
owner. Descendant terminal facts and active-claim release commit atomically,
fencing actual late signed submissions. No feature plugin owns a separate
cancellation path, and no scheduler or authoritative table is added.

This corrects new completion transactions. It does not migrate historical trees
already missing descendant terminal facts; authorized repair remains a stable
qualification gate. Read-only rebuild and audit do not publish replacement facts.

Existing lifecycle decision: [ADR-010](../docs/adr/ADR-010-task-lifecycle-terminal-projection.md).
