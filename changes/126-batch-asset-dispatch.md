# Change 126: Bound repeated Asset-batch Contract dispatch

## Problem

Asset batches repeatedly visited the same Contract set for activation, output
and method-result selection. Breadth-first cancellation also shifted a list
for every front removal.

## Resulting behavior

The existing breadth-first walk uses a deque while preserving child order and
traversal decisions. A nonpersistent multi-value index is built once per
nonempty reducer invocation. It preserves raw activation matching, exact
`created_by` checks, Asset/Contract event order, same-name values and the full
Contract overlay needed by descendant traversal.

The canonical output policy still checks actual Assets and exact independent
qualification. Empty batches preserve their original Store capture behavior.
No Store interface, persistent cache, claim, authority or terminal semantics
change. The index reduces dispatch selection; it does not remove full-history
reads or completion's graph and ancestor traversal.

## Verification scope

Regression must compare event and materialized identities against the full
reference, including same-name/pending overlays and verifier branches. Helper
counts alone do not establish semantic equivalence or whole-run scaling.
