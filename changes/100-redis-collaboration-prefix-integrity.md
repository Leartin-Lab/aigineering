# Redis collaboration verifies its SQLite prefix before relay

Redis synchronization now compares the full bounded stream prefix with the
selected SQLite RuntimeRecords under a watched stream/metadata fence. Small
suffixes append in one transaction. Larger suffixes stage the complete source
projection and publish it with one prevalidated Lua swap. Ordinary sync refuses
damaged, divergent, or ahead projections without clearing them; explicit
`aig redis rebuild` watches the live fence before opening a fresh snapshot of the
routed Store path. Its first attempt may repair the captured damaged or ahead
projection; every retry must prove the new live state as a prefix of a fresh
routed snapshot. Large staged projections apply TTL atomically with each bounded
page, and the final swap checks the exact staged event count. Empty history keeps
both live keys absent.

Status reports an ahead projection as noncurrent. Event reads from an older
snapshot return only its source-known prefix, and wait uses Redis wake events as
hints while polling the selected Store revision. Explicit rebuild keeps ownership
of the original and fresh snapshots in the CLI and uses the fresh snapshot for
the query rebuild and final status.

The default EngineWorker now gives each disposable inner invocation a distinct
Genesis domain, so independent histories do not share a relay namespace. An
explicit inner Store factory retains its existing domain and replay behavior.

Focused collaboration, event, and Redis CLI tests passed, including native Redis
checks. Query-generation correctness and
large-scale resource qualification remain separate work.
