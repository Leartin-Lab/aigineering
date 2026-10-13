# Consistent Redis query generation source

Full Redis query generations now derive every entity and graph index plus the
revision from one consistent SQLite snapshot. A concurrent later commit leaves
the generation coherently older so existing freshness checks can catch it up;
it cannot label a mixed multi-read view as current. The snapshot copy closes
after projection, including on failure, and a failed build publishes no partial
generation.

The snapshot builder requires a source that provides an independently closable
consistent snapshot. SQLiteStore reuses its existing in-memory SQLite backup.
Copy cost scales with Store size and applies to full generation rebuilds only,
not ordinary current reads or incremental Redis catch-up. This change adds no
SQLite schema or Redis authority and makes no latest-read linearizability claim.
The Redis query schema version is unchanged, so a Store that may contain a
generation built by the earlier multi-read capture needs one `aig redis rebuild`
after deploying the corrected code. Only generations rebuilt by the corrected
code carry the consistency guarantee.

Validation: focused Redis query projection and architecture tests, Ruff, format,
and diff checks.
