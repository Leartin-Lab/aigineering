# Change 117: Snapshot recovery source preparation

Method recovery now prepares its signed source binding and ancestor bindings
from one consistent SQLite snapshot when the Store supports snapshots. This
keeps unrelated concurrent commits from invalidating the frozen query midway
through proof. The snapshot is closed after preparation, and the frozen
recovery history no longer retains its query context; the resulting recovery
Candidate is still published through the original live Store and publisher.

Stores without a snapshot capability retain the existing trusted in-memory
path. A snapshot operation that exists but fails propagates its error rather
than falling back to live reads. Generic query matching, publication checks,
and source-binding mismatch rejection remain unchanged.

The regression uses an independent SQLite connection to publish a signed Asset
between history capture and source proof. It verifies both proofs use the same
snapshot query and revision, the copy closes before live publication, and a
genuine source-binding mismatch still fails without publishing recovery work.
