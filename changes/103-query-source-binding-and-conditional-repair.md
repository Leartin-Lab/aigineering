# Change 103: Bind Redis query generations to exact source history

Redis query generations and JSON views now carry an ordered RuntimeRecord-prefix
certificate in addition to their revision and query digest. The certificate uses
effective record payloads and bounded 256-revision windows. It retains a zero
seed and at most 16 recent checkpoints keyed by actual record revisions, and
requires the requested nonzero watermark to exist as the terminal row while
allowing interior gaps. If a live Store revision falls below its cached append
tip, the memo resets before an out-of-range request is refused; arbitrary in-place
Store mutation remains unsupported. Same-revision Stores with different histories
no longer share cached query data. JSON cache keys include
the certificate, and a JSON writer touches watched metadata in its cache-fill
`EXEC` so concurrent generation cleanup detects it.

The query namespace advances to v3. Explicit rebuild requires a fresh routed Store
for repair, validates the new snapshot and source prefix together, and publishes
through one fixed Lua operation that marks metadata non-ready before replacement
and ready only after payload and metadata are complete; old target keys are
passed from the watched scan and removed with `DEL` groups of at most 128 keys.
Catch-up uses the same fence. A WATCH conflict removes explicit-repair privilege
before retry. Reads refuse non-ready metadata. Redis Lua command errors do not roll back prior commands,
so a failed publication may leave a partial target that remains fenced until an
explicit rebuild succeeds. CLI sync/rebuild report bounded source-conflict and
publication-failure results.

The change does not add Store authority, schema, task state or scheduler behavior.
Cold prefix identity scans add work, and the existing large-store full-rebuild
memory limitation remains unresolved.
