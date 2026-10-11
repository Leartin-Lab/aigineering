# Advisory task priority development verification

Date: 2026-10-05. Source version: **0.5.13.dev0**. This records bounded
development verification of [injected fact ownership](../changes/052-injected-direct-fact-ownership.md)
and [signed advisory priority](../changes/053-advisory-task-priority.md).
Deterministic and installed gates pass. The wider Kanban goal, actual-provider
output and stable qualification remain open.

## Implemented behavior

Trusted injected handlers declare their complete direct fact set and selective
exclusive ownership. In-tree registries are strict; external default-`None`
typed and tuple handlers retain trusted compatibility. Invalid projection
containers, members and canonical identities reject durably. Exact single-effect
compound replay proves the original receipt, committed head and ordered member
records before allowing only the documented generic co-effects outside the
direct set. Reserved ownership applies to every record. These configuration
rules do not sandbox Python callbacks. See [ADR-040](../docs/adr/ADR-040-injected-direct-fact-ownership.md).

Priority changes form a signed, exact-predecessor history with values -100 to
100. Zero is the default or clear; a clear retains the head. Exact keyed replay
requires the same authenticated Candidate and currently valid signer. New
no-ops, stale heads and terminal-task changes reject. One application service
orders only the supplied task IDs by priority, declaration revision and full ID;
broad pulls and existing scoped run, fleet and engine enumerations share it.
Runtime validates a complete permutation, retains the original Contracts and
preserves explicit pins and all eligibility/claim fences.

Status, show, list and board expose the effective value and full head. Explicit
`--sort priority` changes list/board ordering. A view with no priority facts can
display zero for legacy materialized tasks; any matching fact still invokes
strict history validation. Mutations, dedicated history reads and priority
ordering remain strict. Redis remains required collaboration infrastructure;
SQLite facts survive pending relay. No schema or Contract identity change is
introduced. Priority does not inherit into successors, reset claims or guarantee
global order, fairness, aging or start times. See
[ADR-041](../docs/adr/ADR-041-advisory-task-priority.md).

## Frozen source and deterministic evidence

The 508-file code/configuration/vector/test map SHA-256 is
`5549c0eedd5b100aa5524f64990130a74b6c57784d4620ea96599f67567e55b0`. Its 207-file runtime map is `59fe529e2b8801788b2290a90a2b3a591f2540eeabdf87674a764861b2d5f89f`.
The source, public-document map and root guide stayed unchanged throughout
serial full2: **2,049 passed**, six dependency deprecation warnings, no skips,
81.64 seconds. Full2 log SHA-256: `4a05d686745a6cb8bd196c895d27332a39024786315b326a55106acf0e011c34`. Ruff check and format pass.

Coverage includes forced two-connection stale-editor contention and signed
claim/priority writer contention, exact receipt/head/member replay and signer
revocation, invalid inputs before provisioning, terminal replay, relay-pending
retention, and fact-bearing corruption that must not become default zero.
Signed application cases exercise scoped ordering, pinned selection, holds,
assignment to another registered Worker, unmet dependencies and HTTP claims.
Existing configured-run/method/harness suites cover their shared entry ownership;
this is not a dedicated real-provider priority case for every adapter.

Separate GPT-6 Luna review ran the generic ownership/replay/hook subset
**183 passed** on its bound predecessor map, whose runtime modules remain
unchanged. Priority/entry review first passed 41 cases and identified missing
nonzero view coverage. After compatibility repairs, its affected subset passed
**162**; the final race-test delta then passed **2** against its new exact hash.
These overlapping counts are not added. The root agent ran integrated full and
installed checks, not a second independent full suite.
Final race review manifest SHA-256: `e9b478f4cc9a5f980f79e16ebd7dbbbebbc6f7b2754884863cd7dc12163829ae`.

The Node consumer passed all 11 current collaboration vector files. Priority
uses three linked signed vectors, -7 → 20 → 0; Python and Node verify bytes,
signatures, identities and ancestry. Vector evidence manifest SHA-256:
`5983f01afc36dc5a85dd1c29d976046b8c0045f94689bc82c8cdd5065e5fddea`. This is wire evidence, not a second runtime or global queue proof.

## Installed artifact evidence

Build1 wheel SHA-256: `21641083717e5fe9e65ae963d29f275d1f7d2f68d0868cc841a5d637a5a2cabf`. All 207 packaged Python modules match
the frozen source. Offline installation and the full CLI runner passed outside
the source tree with isolated domains, actual Redis and local HTTP fixtures.
The main schema21 reconstruction preserves 536 records and its semantic
materialization digest; main manifest SHA-256: `bf95b355a930000917dcebdd105e71fad278933f8a81cefef12a8d018ff9b809`.
This does not assert raw SQL table-byte equality: the diagnostic reports changed
Worker registration/claim table fingerprints separately.

The installed priority fixture checks direct negative integers, clear/head
history, no-publication input rejection, exact replay after edits and terminal
closure, nonzero list/board/status/show values, explicit sorting and unchanged
task-list default order. A real signed Worker pull claims the highest eligible task and
skips a higher held task. Editing a claimed task preserves its claim. Exact
priority event references and histories survive scoped Redis rebuild; selected
claim snapshots and record IDs also survive SQLite rebuild.
Priority manifest SHA-256: `cf2c4c3ae6e9abbbc7d29e4f9c4ed59b8587a0cd6b8b1c9b09c074ff62ca5daa`.

The installed generic fixture passes 11 declaration/compatibility/malformed
projection/reserved-owner cases and duplicate-owner rejection. Rejected cases
retain durable rejection without direct facts or a committed head. Ownership
manifest SHA-256: `5c81c80eea932f17e2b1c42921427faeb99771b992ef216dd09ac4b0b10ddcc6`. Both new control fixtures make **zero
provider requests** and produce **zero business outputs**. Other installed
runner outputs are explicit fixture-backed execution, not model-quality evidence.

After public verification documentation changed, the refreshed wheel SHA-256
is `9cb1740d380e3852e2589eb60889c62fd03a64c69b29415e862808d2b051cbff`.
Only its `METADATA` and `RECORD` entries differ from build1; all runtime bytes
remain identical. A fresh offline install of that wheel outside the source
tree reran both control helpers successfully, with zero provider requests;
manifest SHA-256: `8733a7976b1091bc63f9b7934c247bc20bc9bf5b7534fa1f8ab11c7fe86a8d15`.
Package checks compare all 207 runtime modules and 162 public Markdown files
against source, check privacy and metadata, and validate 62 root-guide references.
Final documentation governance passes 48 cases. Documentation updates do not
change the source/configuration/vector/test map validated by full2.

Reproduce with a configured isolated local Redis test service and development
dependencies installed:

```bash
ruff check src/aigineering tests
ruff format --check src/aigineering tests
pytest -q
python -m build --no-isolation
python scripts/installed_smoke.py --wheel dist/aigineering-0.5.13.dev0-py3-none-any.whl --expected-version 0.5.13.dev0
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/task-priority-vectors.json
pytest -q tests/architecture/test_governance.py
```

Set `AIG_REDIS_TEST_URL` and `AIGINEERING_REDIS_URL` to the isolated service;
each run uses fresh domains. Never flush shared data. Keep private diagnostic
stores outside public release artifacts.

## Retained failures and open gates

Full1 retained **30 failures and 2,014 passes**, six warnings, 81.52 seconds;
log SHA-256: `8bd6e817c097ec32f35135d486b7a0ef9b0ee8b1eff15f3328aaf91b7b3a2fbe`. Unconditional strict priority history reads broke
legacy fact-free views and suppressed the existing integrity/status report.
The shared presentation helper now skips strict history only for exact IDs with
zero matching facts. Old architecture/CLI test hooks were updated to the shared
claim wrapper, and broad Worker fixtures now seed canonical signed declarations.
Imported continuation and malformed-policy cases retain explicit pins where
broad discovery is outside their test purpose. Full1 is failure evidence; view
coverage and installed helper changes made during that invocation are not
attributed to its passing subset.

Earlier focused failures are retained: a missing isolated Redis URL, sandboxed
Redis access, an overbroad race-test gate on rejection persistence, an incomplete
Node fixture, and a null-target claim draft with a target-specific ancestry
assertion. Corrections are distinguished from runtime defects. The first
independent metadata transcription errors were corrected with original files
retained; they are not additional passing runs.

No real model-provider call was made for this slice. The actual Flash HTTP401 in
[report 082](082-task-assignments-2026-10-05.md) remains unresolved, with unknown
usage/cost and no accepted report; its API Base URL still needs confirmation.
Task edit/promotion, bulk operations, boards, dispatch/scheduling, notifications,
retention and PR acceptance remain separate work. No commit, remote update,
release tag or publication was performed for this slice.
