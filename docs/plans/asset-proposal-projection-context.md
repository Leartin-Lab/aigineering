# D1a: bound projection context for homogeneous Asset proposals

Status: corrected frozen five-file implementation independently Passed after the retained governance finding; root full/installed/build and separate independent overall review Passed; final metadata/source-package light closure is tracked in report113. Original plan snapshot `01be0d369671ebbbc3091f2f463ac261c8d0fe62e626beaf34aac9196f80e3b2`. Initial inventory: Luna `recheck_tools`; revised plan: Luna `provider_plan_v2`; plan audit and subsequent implementation author: Luna `plan_audit_v2`. Root, not the five-file author, independently reviewed implementation. Separate overall integration reviewer: `integration_review_v2`.

## Source review and decision

The bounded slice is viable and useful if its claim is restricted to the Candidate effect-projection context stage. `CandidateCommitter.commit` currently calls `load_contract_declaration_projection_context` before authentication and before pure reduction (`core/commitment.py:197-218`). That loader only recognizes homogeneous `contract.declare`; other effects fall through to `load_effect_projection_context`, which materializes every Contract, Asset, RuntimeRecord, WorkerRegistration and claim-history ID (`core/projection_context.py:114-159`). D0 measured that exact fallback increasing with unrelated closed history. Its independently checked P=64 sample had `full_projection_context` materializing 403 rows at H=16 and 1,603 at H=64 for the batch, and 25,792 / 102,592 for 64 sequential publications. Those are returned/materialized rows, not physical scan rows. Evidence: the D0 full-matrix report digest `282737ed138b08108637ce3ade518ba98d3a16f532dd8dcc1a548219726a67f4`; independent verification JSON digest `9d0dc4636a393ca3ff2bae2b3708b0380e9c5d0e5818899012cbd7323ec0b557`.

`project_asset_proposal` itself reads the exact claimed Contract only for a claim-bound Candidate, to check output membership, creator provenance and the parent's protected-name delegation (`core/effect_projectors.py:126-164`; `core/effect_projection.py:203-255, 342-343, 490-542`). Ordinary unbound operator Assets read no existing Contracts or Assets. A pure `asset.propose` does not emit a graph assertion; it emits `asset.committed` and a legacy compatibility graph record. The generic projection still uses RuntimeRecords for exact single-effect replay (`effect_projection.py:278-286`, `injected_fact_replay.py:27-99`), but only the exact existing receipt, all matching committed heads, and every member named by those heads are relevant. For a multi-effect asset-only batch, replay detection is disabled by its existing `len(projections) == 1` guard. The return validator is a no-op unless pending records contain `allowance.returned` (`allowance_returns.py:260+`), and allowance grant projection returns immediately when there are no projected Contracts (`causal_allowance.py:108+`). `project_new_graph_assets` only needs historical definition/content records when the current projected batch contains `asset.definition-content.asserted`; the pure Asset-proposal shape cannot produce that fact.

Therefore, request a narrow optional reader for homogeneous built-in `asset.propose` Candidates. It can provide an empty context for unbound proposals, the exact parent Contract for claim-bound outputs, and the existing exact-replay bundle for single-effect candidates. No new fact, authority, context-selection rule or scheduler is needed. Keep the optimization out of `FactReducer` and the claim owner. This is a local reduction in one measured cost stage, not an end-to-end H-locality fix: `CandidateCommitter.commit` still reduces Asset effects before commitment (`commitment.py:234-246`), and SQLite reruns Asset consequences under `BEGIN IMMEDIATE` (`sqlite_store.py:3161-3430,3422-3472`). Those pre-commit/writer reducer costs and broader task completion/cancellation cascades remain H-sensitive and outside D1a.

## Proposed contract and exact read inputs

Freeze this exact optional API in `core/projection_context.py`:

```python
@dataclass(frozen=True, slots=True)
class AssetProposalProjectionRequest:
    candidate_id: str
    claim_bound_contract_id: str | None
    include_candidate_replay: bool

@dataclass(frozen=True, slots=True)
class AssetProposalProjectionInputs:
    contracts: tuple[Contract, ...] = ()
    runtime_records: tuple[RuntimeRecord, ...] = ()

def build_asset_proposal_projection_request(
    candidate: CandidateProposal,
) -> AssetProposalProjectionRequest | None: ...

def load_asset_proposal_projection_context(
    store: StoreProtocol,
    candidate: CandidateProposal,
    *,
    actor_keys: tuple[ActorKey, ...] = (),
) -> EffectProjectionContext: ...
```

The only optional Store method is exactly `read_asset_proposal_projection_inputs(request: AssetProposalProjectionRequest) -> AssetProposalProjectionInputs`; SQLiteStore implements it, it is absent from `StoreProtocol` and `CandidateCommitStore`, and the loader detects it by `getattr`/callability. The exact call site is `CandidateCommitter.commit`, at the existing pre-authentication projection-context selection currently calling `load_contract_declaration_projection_context`/`load_effect_projection_context`; route only this hint through the new loader, leaving all other dispatch and `reduce_candidate` ownership unchanged.

The builder accepts only an actual `CandidateProposal`, with `effects` an actual nonempty tuple whose every member is an actual `CandidateEffect` and whose `effect_type` is the exact string `"asset.propose"`. It does not inspect payload, signatures, capabilities, or policy. `candidate.id` must be an exact nonempty string. `claim_binding` must be `None` or an actual `CandidateClaimBinding` with exact nonempty string `contract_id`; otherwise return `None` and use the original full-context route. `include_candidate_replay` is true iff the tuple has one effect. Empty effects, list/other/malformed collection, non-string/opaque effect type, wrong effect instance, malformed claim binding, mixed effects, and all unsupported cases return `None`.

The builder catches only `KeyError`, `TypeError`, and `ValueError` arising from this hint's typed-field extraction and treats them as uncertainty (`None`); it does not catch `AttributeError`, SQLite/reader errors, or unknown/programming exceptions, which propagate. The exact allowlist applies only before calling the optional reader. Once selected, reader errors propagate unchanged. No request is a full-context fallback before authentication, so malformed/forged input preserves original authentication/rejection ordering.

When replay selection is requested, return every matching `candidate.received` and `candidate.committed` record for the ID, preserving multiplicity and revision order, then return every stored member named by every matching committed head. Do not pick one head/receipt, canonicalize malformed member containers, or suppress duplicate/missing members in a way that changes `exact_single_effect_head_replay`'s false result. For list/tuple committed member IDs, the indexed `record_id` lookup may de-duplicate only the physical lookup set; the original head remains unchanged for the original uniqueness check. Non-string/opaque member containers still return the head and no inferred members, allowing the existing exact-replay predicate to decline replay as before. Query candidates through the already-used exact candidate ID expression/index path and fetch members by exact record ID. Preserve the current revision ordering of the selected context. No replay selection means no candidate receipt/head/member query at all.

Treat this as an optional Store adapter capability, not a required `StoreProtocol`/`CandidateCommitStore` method. A custom or Memory Store lacking the reader uses the existing full context. `SQLiteStore` runs exact selectors on its own read snapshot when no transaction is active and uses the caller's transaction when one is already active, matching declaration-reader transaction behavior. Database/adapter errors propagate unchanged; only the pre-reader hint allowlist falls back. Do not catch reader `TypeError`, `ValueError`, SQLite or programming failures. No new SQLite table, cache authority, registry, or schema migration is proposed.

The following stay on the existing full context: any mixed effect batch; `asset.assert`, content/definition/definition-content graph effects; `contract.declare`; any injected/custom effect actually present; unsupported or malformed effect collection whose shape is uncertain; and a Store without the optional reader. Merely registering an unused additional effect remains compatible with the pure built-in Asset path, as the existing declaration reader is independent of unused handlers. Never treat an empty context as proof that a Candidate is authorized. Candidate authentication, required capabilities, reserved/protected-name checks, actor signing, replay, claim validation and commitment remain with their existing owners.

## Ownership and files

Proposed implementation ownership, subject to the independent plan audit:

- `src/aigineering/core/projection_context.py`: immutable request/result types, conservative hint builder and optional-reader loader.
- `src/aigineering/core/sqlite_store.py`: exact indexed reads and read-snapshot lifecycle; no generic SQL in projector code.
- `src/aigineering/core/commitment.py`: dispatch the existing pre-reduction call through `load_asset_proposal_projection_context` for its exact hint, preserving `reduce_candidate` as the semantic owner.
- `src/aigineering/core/store.py`: no change expected; the adapter stays optional. If an implementation requires making it part of a structural protocol, stop and return to review instead of silently widening the Store contract.
- New `tests/test_asset_proposal_projection_context.py`: read selection, context/differential behavior, signed H matrix and transaction/fallback cases.
- `tests/architecture/test_asset_proposal_projection_ownership.py`: the sole architecture fence file for this package. Assert the optional method is absent from `StoreProtocol` and `CandidateCommitStore`; SQL selectors live only on SQLiteStore; projectors/reducer receive no Store; commitment dispatch stays before the same reducer/authentication owner; unsupported shapes keep full fallback; and built-in `asset.propose` cannot be overridden by injected handlers.
- A small public `changes/NNN-...md` is not a prerequisite because this changes no behavior contract or authority. Add one only if final implementation materially changes public architecture truth; no ADR is indicated by this local read optimization.

`core/commit_snapshot.py` needs no change for this slice: `build_candidate_projection_snapshot_validator` recomputes only when an injected effect in the Candidate was handled (`commit_snapshot.py:129-163`), and the narrow shape excludes injected effects. SQLite's writer transaction continues to run all existing claim, Asset, allowance and terminal fences. No projector or reducer receives a Store handle.

## Behavior and failure tests

Start with tests that fail against the full-context behavior, then implement. Use real signed Candidates and accepted closed-H setup with isolated SQLite Stores; never synthesize accepted history with raw RuntimeRecord appends. Compare `reduce_candidate` using narrow context against the same authenticated Candidate reduced with `load_effect_projection_context`, comparing acceptance/rejection, rejection reason/category, Contract/Asset IDs, RuntimeRecord semantic payloads/IDs and trace semantics while ignoring only existing display timestamps.

Cover the following fixed groups and cases:

1. Ordinary unbound Asset-only operator input: narrow context has no Contracts, Assets or unrelated RuntimeRecords; the candidate is accepted with the exact existing Asset signature/content/creator facts and the same result as full context. Include multiple `asset.propose` effects and a protected/reserved-name refusal to ensure the hint does not grant capability.
2. Claim-bound business output: return exactly the bound parent Contract and no other Contracts/Assets. Exercise valid exact outputs, missing parent, undeclared/wrong output, wrong `created_by`, method-result `created_by` rule, output-shape policy failure, protected output delegated by the parent, and parent without that delegation. Existing claim writer checks and `claim.submitted` behavior must remain unchanged.
3. Exact single-effect replay: complete accepted replay is recognized from exact receipt/head/members; different effect data, missing member, duplicate head/receipt and malformed member list do not become accepted replays. Compare against full-context results and prove the selector returns all matching rows rather than choosing one.
4. Scope fallback: mixed Asset+Contract, explicit asset graph/assertion, custom/injected effect in the same Candidate, malformed collection shape and a Store without the optional method hit full context. An unused registered handler does not force fallback for the pure Asset-only shape.
5. Authentication/error order: forged/bad-signature Asset-shaped Candidate still reaches the existing authentication rejection owner; no parser/projector rejection is emitted by the hint builder. Known uncertain hint shape returns to full context. Reader SQLite/runtime errors and unexpected programming exceptions propagate, rather than being converted into Candidate rejection or broad fallback.
6. Generic consumer inventory: exercise `_validate_claim_bound_effects`; all selected effect projectors; `exact_single_effect_head_replay`; reserved/direct-output validation; `_validate_graph_batch` and `project_new_graph_assets`; claim-bound output/protected-name checks; `validate_causal_allowance_returns`; `project_contract_allowance_records`; and assignment validation. For pure `asset.propose`, prove graph assertions and projected Contracts are absent and allowance grant/return paths take their existing no-op branches; no unrelated history is requested.
7. Replay history matrix: exact single receipt/head/member bundle; multiple receipts; multiple heads; malformed/duplicate/missing/ambiguous head/member history; duplicate/missing member IDs; opaque member container; revision ordering. No-replay case performs no replay reads; fixed-M replay returns exactly matching receipts + heads + members.
8. Claim-bound matrix: missing/wrong parent, undeclared/wrong output ID, wrong creator, method-result creator rule, output shape, parent-delegated protected output and missing delegation all match the full-context projector result.
9. Hint/fallback/error matrix: empty effects; malformed/non-tuple collection; non-string/opaque effect type; wrong effect object; malformed claim-binding shape; mixed Asset+Contract; graph/assertion Asset; injected/custom Candidate effect; Store without optional reader; unused registered handler; malformed and forged Candidates. Assert only the documented hint exceptions fall back; reader `TypeError`/`ValueError`, SQLite errors and unknown exceptions propagate unchanged. Authentication rejection order remains unchanged.
10. Atomic publication and rebuild: ordinary and claim-bound cases commit through `CandidateCommitter` and existing SQLite writer checks; reopen and strict rebuild preserve Asset seals, committed IDs, traces, claim state and materialization digest. No writes occur during selection. Verify caller-transaction and owned read-snapshot behavior.

Differential comparison binds identical signed Candidate bytes and compares narrow-context and full-context `reduce_candidate` results: accept/reject, reason/category, Contract/Asset IDs, RuntimeRecord semantic payloads/IDs and trace semantics, excluding only explicitly documented display timestamps. An empty context is never treated as authority.

Add/extend one architecture assertion that the optional capability remains absent from required Store protocols, the pure projector contains no Store access, and SQL selectors reside in the SQLite adapter. Do not assert source line numbers or a particular SQL string if an equivalent exact indexed selector passes the behavioral and query-plan gates.

## Locality acceptance and evidence

Freeze one exact post-C3b source snapshot before implementation because `sqlite_store.py` is shared with C3b. D0 is a historical baseline on its exact bound source, not evidence for a changed tree. Recreate the D0 signed closed-H context case on the implementation snapshot at H=0/16/64 with fixed P=64 pure `asset.propose` batch and identical measured Candidate bytes; include a separate P=1 replay selector case. Instrument only test-side wrappers around the actual request reader: SQLite traced SELECT count, actual returned rows, `_row_to_runtime_record`/Asset/Contract decoder counts, projection-context tuple lengths and query-plan/VM proxy. Report each context-reader measure separately from pre-commit `reduce_asset_facts` and SQLite writer `reduce_asset_facts`.

For the P=64 unbound batch with no replay history, selected Contracts/Assets/RuntimeRecords and decoded rows are exactly zero at every H. For P=1 with no candidate history, returned rows are also zero. A claim-bound output selects at most one exact parent Contract. For same-ID replay, explicit replay size M is fixed across H; returned/decoded counts equal the exact matching receipts + heads + members and remain constant with H. Freeze measurements as test-side instrumentation of actual selector statements: SQLite trace callback for SELECT counts, explicit returned-row counts, decoder counters, `EXPLAIN QUERY PLAN`, and SQLite `set_progress_handler(callback, 1)` instruction-step counts (the same repository method already used by the allowance projection-selection tests) for the exact parent Contract, candidate receipt/head, and member selectors. Each selector must show indexed `SEARCH` on its exact key, never an unrelated-history `SCAN`. At fixed references and M, selected/returned/decoded object counts are H-invariant. B-tree VM work may grow logarithmically: require each selector at H=64 to satisfy `VM(H=64) <= 2 * R0 + 64`, with R0 the same selector measured at H=0 for the identical request. Query set and plan interpretation are fixed and emitted; missing indexed SEARCH or any bound violation fails locality acceptance. These finite VM measurements are not a complexity proof. Report pre-commit and SQLite writer `reduce_asset_facts` separately; their H cost remains explicitly open and outside this threshold.

Use the D0 report's selected P64 materialized rows only as baseline context: batch total SQL rows were 91/638/2,270 for H=0/16/64; sequential totals 18,046/53,054/157,502. D1a's acceptance is limited to the projection-context stage. It does not promise equal end-to-end totals, zero H reads by the FactReducer, or a performance win in total commit latency. Do not use finite samples to claim overall complexity or physical scan counts. Author checks run `pytest -q tests/test_asset_proposal_projection_context.py tests/architecture/test_asset_proposal_projection_ownership.py`, `ruff check` on the three changed source files and two owned test files, and matching `ruff format --check` on those paths. The independent reviewer reruns focused tests and architecture fence against the exact frozen source; inspects the complete consumer/failure matrix and indexed query plans/VM measurements; and binds stdout/stderr/exit plus exact source hashes. Root runs the applicable integrated SQLite/full gates after integration. Preserve red runs; author checks are not independent acceptance.

## Dependencies, risks and open gate

Plan dependency: current D0 independently Passed evidence and the finalized D stage scope. Implementation dependency: a root-declared frozen source after C3b's SQLite integration gate, since this package edits the same SQLite adapter. Integration dependency: rerun the accepted context and Asset writer tests against the combined C3b+D1a source, then root's applicable SQLite/full checks. This draft does not assert that C3b is already integrated or passed in the moving tree.

The main risk is missing a context consumer in the generic batch projector. The exact consumer inventory above must be independently checked against current code before Ready. If additional effects or an output shape require any full-history data, retain the full fallback rather than expanding this slice. A further risk is accidentally treating Candidate-ID replay selection as exact replay proof; it is only an input selection, and the existing `exact_single_effect_head_replay` remains the only proof owner. Another is reporting local read counts as whole publication cost; phase-specific metrics and explicit exclusions prevent that overclaim.

The original proposal above was approved independently before implementation; the exact delivery record below supersedes its historical draft status.

## Reviewed source/document snapshots

| File | SHA-256 |
| --- | --- |
| `docs/development-workflow-adapter.md` | `3d33000a69fe720c181d9902bc205b0d50692c93ae35a8dd85a5dd5264dd81ce` |
| `docs/plans/sustained-engineering-delivery.md` | `8fe354baeeafbad069c40707a0cc42b07a9df3847eecacb96e863d8bebd4fcf5` |
| `docs/plans/asset-publication-locality-baseline.md` | `8d79af2a925f16424d2687e0fc0d7e8dfbb2e41571845d6b6a2802523e55f0b1` |
| `DESIGN.md` | `f1dd0dbe5b888afcefc81d494a54dad9895d1f13f10b022988e8960fc52f66c1` |
| `docs/boundary-invariants.md` | `18e0bab39994b304d804eb6fb5ab1db4f6cd9ce30616269bce303e4e8b86d93d` |
| `src/aigineering/core/projection_context.py` | `de2dd2976d7f2d3117559174bbf5e486f1aa2b4ecfeade5adb9271e857002d62` |
| `src/aigineering/core/effect_projectors.py` | `65c93b7777adbc665be92fdd14c00a99efec200c7532c6de89fa1e6ca1c684d0` |
| `src/aigineering/core/effect_projection.py` | `ab4b012e84aefa0cb9ad0b2e717f4879bf9eed09d024fe56db9747b11fcf6aa8` |
| `src/aigineering/core/commitment.py` | `1975eca3235fa39b17b068c61369c02072546bd938720fbf04371b16be9c69f1` |
| `src/aigineering/core/commit_snapshot.py` | `87eb80d218d68fda1121f3b9c6ec3ce7b66171cd2040ae4d5a43188ee36ccca6` |
| `src/aigineering/core/sqlite_store.py` | `afa7dc7fd98ad2bc284102ab6fe5d5f93ee9cc18e6906ccec83030395b808ba9` |
| `src/aigineering/core/store.py` | `49c3341452fa1fb0e4748da23980b211279cacaff6de75408dfcc03dbf16146c` |
| `scripts/benchmark_asset_reducer.py` (D0) | `bea8dec74ca42334726b1f78fd1e4dd866a438fdc3a935274bc5e34d685e4a66` |
| `tests/test_benchmark_asset_reducer.py` (D0) | `09920cc0b76cb843f1d25482929593384ec715c2b4bab7384b40379f6e61ed46` |
| D0 independent matrix verification JSON | `9d0dc4636a393ca3ff2bae2b3708b0380e9c5d0e5818899012cbd7323ec0b557` |

## Frozen implementation acceptance

The post-C3 start source is `3d2e8153085b6331e7d122cca097c9b62d4c600a`, with 711 code/test/script/fixture files at `9dbc004e376e2fb3fa8e6e7c597cbdfb4958edd2a222f1d0b6798697224cff65` and 236 Runtime modules at `b048dd0563a01f6e9c815edd318c5284db317654b4b2f1fdc44a3847f6dc78e9`. Independent plan approval is `148a54f2c07c485f70c32e2f2cfd75439d633ec5b3d764c2395a0e3d23c6ae22`. Frozen five-file implementation map is `9ece0d4bf29bc48e48c0c1f068f71c941166d3d18263361335e2d4dc0b2d9b72`.

Root independently reconstructed that base plus only the five owned files. Its 164 affected/architecture tests, 33 normal signed native-graph differential vectors and five actual reserved method-prefix refusal vectors passed. Twelve selector probes used D0's normal signed claim/execution/completion history with two historical Assets per closed task. At H0/16/64, actual returned SQL rows were respectively fixed at 0 for fresh single and P64 unbound, 1 for the exact parent, and 4 for a receipt/head/two-member replay. Decoder counts agreed, queries used indexed SEARCH, read selection wrote nothing, and interval-one VM counts were 55/6/37/125 at every H. Finite measurements are not a whole-system complexity proof.

Independent review `501ffe77ebe5a2f461914b1f77276b52d8bcde235bdce77a5bbd245a8acf351d` Passed this implementation only. Its exploratory raw malformed-graph setup was rejected before comparison and is not a production defect or feature proof. A separate D0 observer run failed 1/5 tests because the new loader was not yet attributed; [observer compatibility](asset-projection-benchmark-compatibility.md) is an explicit integration dependency. Pre-commit and writer FactReducer costs, claim discovery, full fallback and broader H-locality remain open. Combined evidence belongs in the [development report](../../reports/113-asset-context-direct-transport-development-2026-10-25.md).

Root initial governance check: 48passed/1failed, because the added dispatch grows the generic commitment coordinator to311lines above its existing <300 fence. Keep that fence; move dispatch to the context owner through one generic factory. The five-file correction and affected D0c entrypoint adaptation require new exact snapshots; earlier passing evidence remains historical.

## Governance correction and new acceptance

The coordinator now calls one generic factory owned by `projection_context`; its specialized loader APIs remain, and a shared private helper consumes the frozen request once. The 297-line coordinator passes the unchanged <300 ownership fence. No compression or test relaxation was used. Correction plan `4c171411d5a4a7d943c50b6cfb369f24c2e5300da44ebf96631fc0e8cae44666` received independent Ready audit `d8a960b69f24eea485c36cd0a9f7754109a0dce02ed0b92cdcc14337e00d99c3`.

Final component map `98876ea5c50744fddef967aba8b913c011cf6e208525e467ce2c27c47e53e84a`, 236-module map `2fe67cd6343846da2d104337d47dbb1ac81da3503c55c0e5ddc668ed8ff6350f`. Root independently reran 216 tests (167 affected plus 49 governance), 33 semantic vectors, five method-prefix refusals and all 12 normal-history selectors, with the same measured row/decoder/VM counts on these new bytes. Separate independent acceptance `34a78b5bff32b6a8086f90fe12bcc5a7553c500148c2eda16385b3a9b05239e6` supersedes the old component's delivery verdict; all previous source/results remain historical. D0c must observe the actual module-qualified generic factory once, rather than its nested specialized loaders.

Root全套首次五例因三个旧测试hook仍指向commitment已移除的导入别名而在writer前报AttributeError。三文件维护只patch实际generic factory，并观察真实pure reduction接受后再验证fresh writer拒绝；非作者五例通过，原生产19文件和所有236模块保持。独立维护review8969d5354e87540218e5c154de884c9b4f60e4527396f858fb740b2f6e1454bc。完整/安装门禁重新绑定35文件组合，首轮结果保留。

Root预声明四切片及三文件hook维护组合：源码3,532项与树外656项、Ruff/格式、构建及安装驱动检查通过。717文件代码/夹具摘要e87654c18b07100d555247e2e15989ffaa28c598e8e602d43158f1c2ee9da9ca；非作者对准确组合独立总体验收Passed；最终验收元数据/源码包按同一717文件代码快照轻闭合，详见report113。以上不是本组件作者单独完成的全套，也不替代本轮未做的真实provider/整体规模资格。
