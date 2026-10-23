Implemented a local fix on `dev` for Report 107’s confirmed loss of structured planning-compile rejection details. Plan and replan failures now preserve bounded, versioned diagnostics through authenticated claims, durable failure/release, existing generic recovery, SQLite reopen and Worker disclosure. Authority, terminal facts and recovery policy remain unchanged. See [change 105](../changes/105-structured-compile-failure-diagnostics.md).

Verification passed:

- **2,933 tests passed, 34 skipped**
- Ruff check and format
- Wheel and source archive build
- **234 installed modules** matched source bytes; **38 behavior tests** passed against the wheel
- Final governance and architecture checks: **50 passed**
- All **95 root-guide references** resolved

A signed two-tool chain succeeded on both unchanged `825b4bd` and the changed tree: Step B consumed Step A’s token and produced the final token after five ordinary invocations, with zero rejections. This disproves universal multihop failure; continuation packages receive observations through method context despite empty inputs.

| Claim | Current recheck |
| --- | --- |
| P0-1 | Recovery metadata/policy gap remains; infinite looping not established. |
| P0-2 | Cancellation is node scoped; tree semantics remain undecided. |
| P0-3 | “No recovery path” rejected; structured-feedback loss fixed locally. |
| P0-4 | Budget-1 compile refusal reproduced; default policy unresolved. |
| P0-5 | Narrow input-authority gap remains; universal failure disproved. |
| P0-6 | Historical repeated-step model result remains unknown. |
| P0-7 | Root-grant refusal already landed; broader funding questions remain. |
| P1-8 | Allowance guidance remains incomplete. |
| P1-9 | Target-run failure exit remains; no new supervisor selected. |
| P1-10 | Session discovery gap remains; task audit facts exist. |
| P1-11 | Selected explicit failures exit 1; empty-state policy unresolved. |
| P1-12 | Scope diagnostics remain limited. |
| P1-13 | Descriptor expressiveness gap confirmed; universal collision unknown. |
| P1-14 | Historical deep-chain result unknown; Worker retry already uses parent/allowance. |

The supplied original has **759 lines**; earlier artifacts cite a different 1,111-line version. Historical CatAgent3/provider results and successful corrected-blueprint execution remain unverified.

Base: `825b4bd210b4ca62ae1b8f7cebacdc555cd6491b`. Final implementation snapshot: `a7e9d15915c7c33b96a86ffd3252e20011472bd79a4060911e12928384d40269`. Exact hashes, classifications and evidence are in the [verification manifest](data/107-compile-diagnostics-verification.json).

Completed within the 40-minute cap using the configured `gpt-6.1-sol` with high reasoning. Checkpoints and failed-run evidence were retained. **Changes remain local and uncommitted:** independent review, required before commit/push, was not performed. No release, tag, deployment or global configuration change occurred.