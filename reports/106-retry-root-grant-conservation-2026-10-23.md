# Root retry grant conservation verification

Development evidence for the root-grant safety slice of P0-7, based on
`8256e8db1f60b68bd4ffdfd4fa6fe69f38ef9287`. The reviewed 14-file snapshot is
`4f3ccd987ea62917e890360ed1c7603858b14a0f8e3a89fd6a8379206e5f22b9`.
[The verification manifest](data/106-retry-root-grant-conservation-verification.json)
contains exact file hashes and the complete delivery file list. This report and
its manifest are evidence additions beyond that implementation snapshot.

New root Contracts with `origin="retry"` cannot receive an unfunded grant.
Projection refuses them and the writer independently checks the rule after
canonical grant provenance validation. Both CLI spellings preserve the existing
rejection audit and relay-pending results; bulk retry preserves per-target
partial results. Parent-funded retries retain their existing reservation rule.
Historical accepted root retries remain readable, rebuildable and exactly
replayable without new allowance. See [change 104](../changes/104-retry-root-grant-conservation.md).

Required source checks passed: Ruff check and format, and the full suite with
2,894 passed, 34 skipped and six dependency deprecation warnings. The package
build produced a wheel and source archive from an intended tracked-source
staging tree. All 233 Python modules in the installed wheel matched the runtime
source byte for byte; 26 retry tests passed with that wheel imported outside
the source tree. The wheel SHA-256 is recorded in the manifest.

Independent Sol audit found no open issue in this slice. Its isolated checks
passed 252 focused tests and the repaired 33-test legacy CLI file, plus Ruff.
The full suite and package checks above are implementer evidence. The audit
reviewed the initial 13-file snapshot and then the additional legacy CLI fixture
delta; all initial file hashes remained unchanged.

Failures were retained: initial lint/format issues, migrated lineage and bulk
result expectations, new fixture setup errors, an independently detected missing
cancellation relay stub, and one older test expecting an unfunded root retry to
succeed. They were corrected and affected tests rerun. The earlier full run
interrupted for fixture isolation is not counted as passing evidence. The later
failed full run had 2,893 passed, 34 skipped and that single old-fixture failure;
the final full run passed after converting it to signed, parent-funded work.
All 95 checked root-guide references resolved.

This evidence uses signed SQLite fixtures, two-writer barriers, controlled relay
failures and historical grant fixtures. It does not claim real provider or native
Redis execution. Original-work funding rights, source-fund disposition,
dynamic budgets, recovery across changed signing contexts and older-version
admission remain outside the completed slice. No stable release, tag or package
publication is established by this development delivery.
