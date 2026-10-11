# Exact claim-attempt diagnostics: development verification

Date: 2026-10-07. Version: `0.5.13.dev0`. HEAD:
`01be3f4e585acd7975284a38e88dbdcbb44724c0`, with inherited working-tree changes.
No commit, remote update, tag or package publication was made.

## Implemented behavior

`aig task runs CONTRACT_ID` reconstructs the immutable task's claim allocation
and proven outcome. `aig task diagnostics CONTRACT_ID --claim CLAIM_ID` lists
safe claim-causal facts. Both support human output, JSON and fixed revision/time
cursors. Redis remains required; inspection preserves SQLite facts and claims.
The application owner and thin CLI adapter introduce no schema, protocol,
publisher or scheduler. See [change 058](../changes/058-task-attempt-diagnostics.md)
and the [operator guide](../docs/reference/task-board.md#inspect-claim-attempts).

Allocation does not prove invocation; submitted output does not establish
independent qualification. A successful task's materialized completion may have
no proven claim-causal parent and is then omitted from diagnostics. Task status
and acceptance views retain their separate roles. Recovery markers likewise
cannot establish completed successor work. Captured logs and tail remain outside
this slice.

## Exact source and independent checks

The author3 manifest freezes six implementation/test/change files. Root checked
their bytes; an independent reviewer verified matching hashes before and after
16 focused tests, six targeted cases and scoped Ruff checks. These counts overlap
other verification and are not added to the full-suite count. Signed renewal
was checked in a TestClient fixture with Redis seams replaced, not a deployment.

The integrated code snapshot has 536 files, SHA-256
`2feaff8b650089beca643b74d22fa7e2b1fb75715d726cf27a726778c541c12c`.
Its 210 runtime files have aggregate SHA-256
`51d265cf733479fc9abe2b21a0e392d03f91aaff6650a87e128d0d6aa2508ab4`.
Full Ruff check and format check passed (461 files). The full integrated suite
passed 2147 tests with six dependency deprecation warnings in 112.93 seconds.
The initial build and installed controls also passed. After the public-document
updates, all 48 governance tests passed.

Independent probes exposed malformed Candidate-ID and timestamp disclosure and
a release incorrectly justified by a foreign task's terminal. Those defects
were repaired and reprobed. Exact type comparison also rejects boolean/float
epochs. Unsafe fields become null with stable integrity issues; original stored
values remain unchanged. Original failures are retained. One review probe's
initial stderr was overwritten; its recovered tool traceback is explicitly
identified as recovered evidence, not the original stream.

The adjacent [provider logger repair](../changes/059-provider-log-privacy.md)
has root independent 128-test evidence. A first 126-pass/two-failure run was
denied local Redis sockets by the sandbox; the formally permitted run used a
separate evidence directory. This tests local transports and runtime behavior.

## Actual source CLI control

The final explicit local-Worker control ran against real Redis with a fresh
workspace/domain. All 48 command phases passed: ordinary signed success,
expected invocation failure, JSON/human inspection, SQLite semantic fingerprints,
complete event-set pagination after an unrelated write, reopening/rebuilding,
and private-marker exclusion. Implementation files matched the author3 freeze
before and after. The helper SHA-256 is
`8726a4a578d9beb2130623db20f6c816fc3358fb0ff19f241410eb092904cf04`.

Earlier helper controls stopped on missing configuration input, a stale hash
guard and return-value handling errors. Their exits and phase-specific streams
remain retained. Partial successful commands are not described as complete
controls. No Redis flush/restart or silent operational fallback was used.

Root then installed the new wheel outside the checkout, extracted the helper
from its sdist and ran all 48 phases against required Redis. The wheel and sdist
contain all 210 runtime files byte-for-byte; the sdist has the complete 175-file
public Markdown inventory. Privacy checks and 70 direct root-guide references
passed. Installed controls retain 11 success events and seven failure events;
reopening/rebuilding preserves fact IDs and timestamps. These explicit fixture
controls are not real-provider productivity evidence. One separate official
model-list authentication request returned 200 and confirmed the requested model;
no completion was requested. A fresh useful report, independent content/policy
acceptance, wider CLI capabilities and stable qualification remain open.

## Initial artifacts

Wheel SHA-256: `e71a15cf74fa6911e89d7a665cdaff97f6b4e04fac5fad8aed4d66168c7fe111`.
Sdist SHA-256: `1a4a26cf2f51af58c9f18f0bf49d59c531882b3b7f4756062a074eb81c075933`.
These initial artifacts include the prior checkpoint of this report. Subsequent
packaging must match the updated public-document inventory; its exact archive
hashes are recorded separately to avoid a self-referential report hash. Runtime
source is unchanged.

## Evidence and limits

Retained manifests include `aig-stage5b1-source-3-20261007.json`, the author3
manifest, independent `review-manifest.json` and its addendum, and the source CLI
control5 result and command manifest. Raw streams, identities and Stores remain
private. Cursor pages bound returned rows; current inspection scans runtime
history. These checks do not qualify all historical corruption or broader
production, provider or stable-release behavior.

## Later real-work finding

A later installed `run --once` produced a real provider output assertion, then
`task runs` returned `grant_provenance_invalid`. The legitimate signed request
had an explicit null Contract selector for domain-wide selection; this initial
view required a pinned selector. The original invocation and failed inspection
are retained. Initial fixture and full-suite checks above did not cover that
entry shape. A narrow application repair and its new evidence are required
before treating domain-wide attempt inspection as verified. No producer retry
or accepted business-report claim follows from this observation.

Later the same day, [change 060](../changes/060-domainwide-claim-diagnostics.md)
resolved that selector mismatch. The new integrated snapshot and read-only
inspection of the preserved real task are recorded in
[report 090](090-submitted-task-diagnostics-2026-10-07.md). The initial evidence
above remains historical; the original report still requires policy acceptance.
