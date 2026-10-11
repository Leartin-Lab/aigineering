# Exact hold promotion development verification

Date: 2026-10-05. Source version: **0.5.13.dev0**. This records bounded
development verification of [manual recovery](../changes/054-exact-hold-set-promotion.md).
The wider Kanban objective, real-provider output and stable qualification remain
open. No release tag or publication is implied.

## Implemented behavior

`task promote ID... --reason TEXT` signs each task's complete observed manual
hold set and releases it atomically, with one attributed `task.promoted` audit.
`task promotions ID` reads its history. Results include exact hold/release IDs,
the signed idempotency key, observed status and remaining blockers. Dependencies,
activation, assignment, capacity, allowance, claims and acceptance keep their
existing owners. A release does not promise readiness.

Fresh promotion requires the canonical declaration, one to 64 exact sorted
unique open holds, a live never-claimed task, and no satisfied nonempty output
obligation.
The plugin uses canonical output satisfaction, including policy qualification.
Writer-locked reprojection rejects changed eligibility or any changed hold set
without partial releases. The generic hold validator admits only bounded
same-kind, same-task transitions with authenticated receipt/head provenance.
`task.promoted` is reserved exclusively to its configured plugin owner; hold
release facts remain shared. These rules do not sandbox trusted Python callbacks.

Multiple targets are independently atomic and can partially succeed; any failure
returns nonzero with individual results. More than 64 holds requires partial
exact unblock while retaining a hold, then promotion of the remaining complete
set. Exact replay requires the original explicit hold IDs, reason and returned
key, current signer authorization and the full original committed head. Later
holds remain effective. Automatic discovery captures a fresh request. Redis
remains required; pending relay preserves accepted SQLite facts. No Contract
identity, Store schema or reader-floor change is introduced. See
[ADR-042](../docs/adr/ADR-042-exact-hold-set-promotion.md).

## Frozen source and deterministic evidence

The final 516-file source/configuration/test/vector map SHA-256 is
`39925e84171d488e2e63356723cd9ce363f46273db51e4d4340237fbace8fa10`.
Its 207-file runtime map is
`118832e69e3229616a55d3e7068728826c386e7d84b8af40620a6e3f3eca942d`.
Source, public documents and root guide were unchanged during the final serial
full3: **2,073 passed**, six dependency deprecation warnings, no skips,
82.37 seconds. Log SHA-256:
`3857c9a8795a8502d6aade37f575978174786f0db728e061d81df2a9e8f9be61`.
Ruff check and format pass.

Tests force a second SQLite connection to place a hold or release a selected
hold after promotion projection and before ingress; both durably reject the
stale promotion without its audit or releases. Other cases cover exact replay
after later holds, partial targets, invalid input before identity provisioning,
terminal refusal, the 65-to-64 drain path, and postcommit Redis synchronization
pending. Actual signed competing typed and legacy handlers persist rejection
without an audit or committed head when attempting another owner's audit type.

GPT-6 Luna independently reviewed the feature and found the initially missing
reserved owner. After correction, its feature subset passed **72** with one
intentional optional Redis skip; a dedicated actual Redis DB14 test passed
separately. The final durable-ownership test delta passed **24** on unchanged
bound hashes. These overlapping counts are not summed. Final independent delta
manifest SHA-256:
`df2989f2d4c3198c5210663fef863d4b953b85db482bdd1b6ae59e3d162f109e`.
Root full and installed checks are integration evidence, not a second independent
full suite.

Python and Node consume the promotion wire vector and validate signing bytes,
signature, Candidate identity and RuntimeRecord identity. Root's Node consumer
passes all 12 current collaboration vector files. Manifest SHA-256:
`0ff0e362bb523070da24c2473e2651c5f3d0f90d7ace5080c07c171cc11a696a`.
This is interoperability evidence, not a second runtime implementation.

## Installed control evidence

Build2 wheel SHA-256:
`7a518986f54427e5286c541b600ad5b5d558ca23495eb2ed1a00d1e151e450e1`.
All 207 packaged runtime modules match the verified runtime. An offline install
outside the source tree runs the helper extracted from the SDIST against fresh
domains and actual local Redis. It verifies multi-hold atomic release, exact
replay preserving a later hold, explicit partial-target results, retained
dependency/activation blockers, held-task signed claim denial and claim after
promotion, and SQLite/Redis reconstruction.

Reconstruction compares durable record IDs and semantic materialization digest,
then exact promotion history and event views. It does not assert raw SQL table
byte equality. Installed manifest SHA-256:
`306dc07026e5fcf1315f44b14d6936e5a603220a8664484e7a7d44a357502500`.
This control makes **zero provider requests** and produces **zero business
outputs**; it is real CLI/control execution with fixed inputs, not model-quality
evidence. Package privacy, metadata and root-guide direct-reference checks pass.

## Retained failures and limits

Full1 retained **one failure and 2,070 passes**: an old frozen-registry test
still expected only comment/block/unblock. Independent review also found the
actual audit-owner omission, which the new metadata assertions had mistakenly
endorsed. Both were fixed. Full2 passed 2,072 before a final test-only change
replaced a pure projection check with signed durable publication. Full3 above
binds that final change; earlier passing counts do not validate it.

The first helper attempt stopped at Redis health/domain initialization and
exercised no promotion. The same endpoint later recovered; the cause is unknown.
A separate first restored attempt had an incorrect activation-blocker oracle.
It was corrected and run in a fresh workspace, with failed evidence retained.
Initial race-test assertion errors and the independent sandboxed Redis attempt
also remain distinct from the successful runs.

Promotion-specific regressions do not cover every legacy claim-history status,
already-satisfied independent-policy outputs, or revoked-key exact replay. Source
checks are present; neighboring tests are not claimed as those dedicated cases.
Claim denial before promotion and claim after it are sequential evidence,
not a forced simultaneous claim/promotion race. Dedicated writer-window
contention here covers hold placement and release.

No model-provider call was made for this slice. The Flash HTTP401 in
[report 082](082-task-assignments-2026-10-05.md) remains unresolved, with unknown
usage/cost and no accepted output. Title/body edit, later Kanban operations and
stable qualification remain separate work. No commit, remote update, tag or
package publication was performed.

Reproduce with configured isolated Redis and development dependencies:

```bash
ruff check src/aigineering tests
ruff format --check src/aigineering tests
pytest -q
python -m build --no-isolation
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/task-promotion-vectors.json
pytest -q tests/architecture/test_governance.py
```

Set `AIG_REDIS_TEST_URL` and `AIGINEERING_REDIS_URL` to the isolated test service.
Runs use fresh domains; do not flush shared data or package diagnostic stores.
