# D1b1 / D1b2: Asset-batch Contract dispatch

**Status:** Both components and the exact later Root combination are independently accepted; final metadata/source-package light closure remains, while the stage goal is open. See the [stage plan](sustained-engineering-delivery.md), [D0 locality baseline](asset-publication-locality-baseline.md), [Change 123](../../changes/123-scoped-asset-proposal-context.md), and [Change 125](../../changes/125-pending-asset-output-policy.md).

## Goal and boundary

D1b1 replaces the front-removal list in `FactReducer._detect_unfinished_children` with a FIFO deque. It preserves breadth-first order, child sorting, terminal and satisfied-intermediate traversal, verifier exceptions, and emitted events. It addresses queue reference shifting only.

D1b2 builds one immutable, invocation-local multi-value dispatch index for each nonempty `on_assets_created` batch. It reduces repeated Asset-to-Contract scans for method-result ownership, activation-token matches, and declared output names. The index does not persist, change Store capabilities, or replace the full Contract overlay used by descendant traversal. Empty batches retain their existing Store capture and build no index.

The shared output-satisfaction policy remains the sole output eligibility owner. Pending Assets are checked as actual values, including same-name business and observation Assets. Activation still uses pending names. Independent output acceptance still requires exact qualification. D1a context selection, projection, authentication, protected outputs, claims, terminal facts, and writer-time revalidation retain their existing owners.

## Delivered slices and dependencies

- **D1b1 plan:** SHA-256 `5592802b3d0ff60ea65da63ad9b29d178cee05971d95f8d3bb4f657e5086e976`; independent Ready review: `5b5d8b898382fb478c56db99d4f3b543d1795b99fe282114d26e51271a3c4bb4`. Its implementation depends on the accepted D1b0 pending-output policy. The exact accepted full1,021-file source map is `9b1b3a0247e8193253dc355ee63bd2a348a294e1bc9248febe9adb592c1d3a33`; the two-file owner map is `54d5cf6356cfc0af1eb18961d295ed2a49b672b846bb69166d7a211dcb8028a6`; independent component acceptance is `66b53f3e9e48547e3285a36ca566fb058fb5fea5ea2d6c8e470027aeaaf82c5e`.
- **D1b2 plan:** SHA-256 `5d1b3e51dc4f8c28e2464d700769761aec0727edf4b8e0e3b9aed48dbb16fad4`; independent Ready review: `e620de0d3d15407704ab54e7041ce515c156a1b23ecd31f56218c3db21fb1bab`. Implementation started from the frozen Root source containing the accepted D1b0 and D1b1 bytes. The final 1,030-file source map is `f6cd432b0bf049bc7fdb7b374453f25b0f79f20afafd673c012278a34986263f`; its three-file owner map is `d7eff3086bf7c4e749f2821b1608d279813f547f8c12fe19aab4d7365930f38a`. Independent component acceptance is `89a66dd9b62f065620d6da4720bd2063ed00bba7911c883808f5a8a791cd920c`.

The component sequence depends on the accepted D1b0 policy and then D1b1 FIFO behavior. Root combination checks are a separate integration dependency. C3 metadata, G0c metering and real-model execution are not D1b1/D1b2 implementation dependencies and are not evidence for these components.

## Evidence and interpretation

D1b1’s accepted signed sibling cases use N=8/16. Across both projection windows they observe 2N queue appends and 2(N+1) FIFO pops; the old list implementation shifted 56/240 references respectively. This establishes the queue mechanic, not total completion-cascade complexity.

D1b2’s five normally signed SQLite cells observe both precommit and writer-side reduction:

| Closed unrelated history H | Batch Assets P | Contracts C | Captured input size L | Dispatch candidates K | Shared output-helper calls per window | Descendant graph traversals per window |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 1 | 32 | 672 | 2 | 1 | 0 |
| 0 | 32 | 32 | 2,040 | 64 | 32 | 0 |
| 16 | 8 | 48 | 1,212 | 16 | 8 | 0 |
| 64 | 1 | 96 | 1,686 | 2 | 1 | 0 |
| 64 | 32 | 96 | 3,054 | 64 | 32 | 0 |

Here H is the count of unrelated Contracts closed through normal signed lifecycle, with their Assets and terminal facts; it is a subset of C. Each nonempty window builds the dispatch index with one C-Contract input pass. K is the measured sum of activation candidates, output candidates, and exact method-owner lookup attempts. In these five cells activation and output each contribute P and method lookup contributes zero. L is a captured input-size formula: string activation length plus split-token count, plus each declared output-name occurrence’s length plus one. L is not a count of parser instructions or executed parser steps.

A separate method-prefix case records eight owner-map lookups, including misses; five produce method-result events. Do not describe eight lookups as eight successful results. A signed completion case preserves the full four-Contract descendant graph: both windows record K=2, three shared-helper calls and one graph traversal with eight Contract yields. The unfinished child is cancelled and the outside independent-verifier branch is preserved.

Full event sequences and materialized IDs match the unindexed reference in the accepted test cases. Candidate1 had 55 passing tests but its independent review was **NotPassed** because required differential and counter evidence was missing. Candidate2 added that evidence and independently passed 60 targeted cases plus Ruff and format checks. These results are separate: candidate1’s evidence gap is not rewritten as a product failure or as the later 60-pass result.

An earlier D1b1 review attempt failed when an architecture source check ran outside the checkout and could not find its source. That attempt remains classified as a failed review run. D1b1 candidate2 resolves the source from the imported module, passes 29 independent outside-checkout tests, and retains the original traversal semantics.

## Remaining limits and integration

The measured bound applies only to dispatch index construction and lookup in the stated shapes: O(C + L + P + K). `_contracts_with` capture/sort, terminal and Asset reads, shared output-helper calls and qualification reads, and completion-triggered graph/ancestor traversal remain visible costs. The zero-graph cells do not represent completion work. No claim is made that the complete reducer is independent of H or linear in total publication work; the pending-output policy and full-history consumers remain outside this optimization.

Both component acceptances apply only to their exact source maps. Root is preparing the G0c/queue/dispatch combination on dev183d9c8, which already contains the delivered C3 mechanisms. Its full-suite and artifact gates passed; independent overall review and final metadata/source-package closure remain pending; no stable-release, installed-artifact, real-model or stage-completion claim follows from these component results.
