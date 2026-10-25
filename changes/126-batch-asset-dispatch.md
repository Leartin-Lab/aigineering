# Change 126: Bound repeated Asset-batch Contract dispatch

## Problem

For each accepted Asset batch, `FactReducer` repeatedly visited the transaction-view Contract set to detect activation, declared outputs and method-result ownership. Large batches therefore repeated dispatch work across the same Contracts. Descendant cancellation also used a list as a FIFO queue, shifting remaining references on every front removal.

## Resulting behavior

D1b1 uses a deque for the existing breadth-first descendant walk and keeps the original child order, traversal decisions and events.

D1b2 creates a nonpersistent multi-value index once per nonempty reducer invocation. It preserves raw `activation.split()` matching, exact `created_by` owner checks, original Asset and Contract event order, and all same-name values. Output-name buckets select candidate Contracts while the complete Contract overlay remains available to descendant traversal. The canonical shared output policy still checks actual Assets and exact independent qualification. Empty batches build no index and preserve their original Store capture behavior.

No Store interface, persistent table/cache, output policy, claim, authority, authentication, terminal, writer, or scheduler behavior changes. The index reduces only dispatch selection; it does not remove full-history reads, pending-output helper work, qualification reads, or per-completion graph and ancestor traversal.

## Verification and limits

D1b1 and D1b2 each passed independent component review against their exact source maps. D1b1 passed 29 independent focused tests, including the two signed wide-sibling cases. D1b2 candidate2 passed 60 independent targeted tests, including both real reducer windows in five signed H/P cells, differential event/materialized-ID comparisons, same-name and pending Contract overlays, method-owner cases, and a signed descendant/verifier completion case. Ruff and format checks passed.

D1b2 candidate1’s 55 passing tests were independently classified **NotPassed** because its evidence lacked required semantic differentials and measured helper/graph detail. Candidate2 supplied those missing assertions and measurements; the two results are not conflated. The exact plans, Ready reviews, source maps and component acceptances are recorded in [the Asset-batch plan](../docs/plans/asset-batch-dispatch.md).

The reported C-pass is the index builder’s input pass, not the complete reducer’s total work. L measures captured input size, not parser execution steps. The eight method-owner lookups include unsuccessful lookups; five produced events. Root’s later combination passed full-suite/artifact gates; independent overall review Passed; final metadata/source-package light closure retains the same724-file code snapshot. This change does not establish H-independent total publication cost, real-model behavior, installed-artifact acceptance, or stage completion.
