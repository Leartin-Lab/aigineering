# Change 129: Index typed Candidate heads during Redis capture

## Result

`core.session` now builds a capture-local, multi-value Candidate-head index from the effective by-ID mapping. Commitment traces select
all exact-string matches in their original global order and resolve exact committed member IDs through the existing recursive reader.
Only Redis `_capture_events()` uses the context; session export, CLI cycle, local-Fleet cycle and audit remain on the legacy Mapping
path.

Non-exact-string Candidate IDs mark the context for the original ordered scan. The builder does not hash malformed IDs or raise early,
preserving lazy commitment-trace errors and direct-trace behavior. Empty-map/`None` semantics, fragment parsing, duplicate effective-ID
coalescing, multiple heads, recursive `visited`, event fields and error propagation remain unchanged.

No persistent cache, Store/SQL interface, Redis protocol, prefix-proof shortcut, fact or authority was added.

## Verification and limits

The exact 1,043-file and four-file maps, plan/Ready hashes and independent review SHA-256 are recorded in the [E1
plan](../docs/plans/typed-trace-reference-index.md). The component passed independent review. The five declared suites reported 49
passed and eight native Redis cases skipped/NotRun because no Redis test URL was set; Ruff and format passed. No real Redis operation
was performed.

Signed sequential H=0/16/64 captures had N=16/416/1,616, T=3/19/67 and legacy T×N visits of 48/7,904/108,272. The context enumerated N
values once and returned/yielded 3/19/67 matching heads. The separately measured batch-declaration family and K/Get/Worker-closure
counts are in the E1 plan. Captures matched legacy event fields and digest.

These are Python capture-stage counts, not physical SQLite rows, SQL VM work, real Redis latency or total warm-sync cost. Full-prefix
enumeration/materialization still visits N records; selected-head sorting may cost O(R log R), and E2 full-prefix proof/deep-readiness costs remain unmeasured. Later Root full/installed/build and dedicated Redis checks were completed; a separate non-author overall review accepted the bounded combination with an unexplained intermittent Fleet incident still open. This change does not close the stage or qualify a stable release.

Later Root combination evidence is recorded separately in [report 115](../reports/115-native-capture-development-2026-10-26.md). Root source tests actually executed the eight native Redis cases; the component review above remains 49 passed plus eight NotRun. The installed combination retains an unattributed intermittent Fleet failure alongside a passing full observed rerun. Separate non-author overall acceptance passed for the exact bounded increment, retaining the original unexplained partial workflow failure.
