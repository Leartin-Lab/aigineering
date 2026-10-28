# Bulk priority missing-target classification

Bulk task-priority operations now distinguish a genuinely absent Contract from selected-history integrity failures. The shared application owner returns the existing not-found classification only when the projection has no materialized Contract and no exact typed declaration or priority fact for that ID. If exact typed history exists without its Contract, the owner raises `ValueError`; ambiguous projections, declaration defects, and malformed priority history also remain unknown failures, so the bulk collector stops rather than hiding corruption. Authentication rejection audit claims do not participate in target lookup.

The same check runs before history resolution and publication in both validation and publication paths. Single-target priority now uses the same not-found classification for a genuinely absent Contract. The bulk collector's existing partial-result, per-target key, accepted-pending, and replay semantics remain unchanged.

Mixed priority/retry outcomes, relay-pending retention and exact replay are
metadata operations. Their fixtures do not prove Worker or provider execution.
