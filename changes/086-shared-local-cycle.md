# Shared bounded local cycle

`aig run --once`, configured `aig run --config ... --once`, and configured
`aig fleet run ... --once` use the shared local-cycle owner. Each command
traverses eligible Worker slots only until its first successful canonical
package claim, invokes that package, then stops package admission. A slot with no
eligible package may be skipped. Worker failure is retained and does not open a
later slot. Maintenance may process consequences of existing facts without
admitting another package.

The shared owner uses the caller's selected Store, canonical maintenance and
Redis synchronization, the shared application claim service, and normal Worker
execution. Status continues to distinguish submitted output, pending independent
acceptance, and completion. Existing incomplete-result exit behavior is
preserved. Non-`once` targeted runs, bounded Fleet loops, and persistent Fleet
service retain their repeated/concurrent execution behavior; cycle completion is
not business completion.

The configured Fleet `--once` human result also shows the existing safe Worker
failure summary and exact claim-bound diagnostic detail. It does not print raw
Worker exception text. JSON output, claim behavior, incomplete-result exit
behavior, and the state-appropriate next step are unchanged. Focused Click tests
cover this presentation path; they do not establish live provider or operational
service acceptance.
