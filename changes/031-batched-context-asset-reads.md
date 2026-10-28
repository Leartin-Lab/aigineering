# 031: Batched context Asset reads

Status: implemented

Worker package disclosure and method continuation context assembly can use an
optional ordered Asset batch read. SQLite reads IDs in chunks bounded by both
900 IDs and the connection's configured SQLite variable limit. Adapters without
the optional method retain the existing per-ID read path; required Store
protocols are unchanged.

The helper returns results in requested ID order, skips missing Assets at the
existing call sites, and filters unrequested batch results. Disclosure still
uses frozen Contract context IDs and retains its existing behavior-asset and
sensitive-input checks. Method context retains event ordering, deduplication,
and redaction. This change does not alter schema, authority, or runtime state.
