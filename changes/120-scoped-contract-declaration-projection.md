# Change 120: Scope built-in declaration projection reads

## Problem

A candidate containing only built-in `contract.declare` effects needed a full Store projection context before reduction, including unrelated Contracts, Assets and runtime history. This repeated materialization as signed declaration history accumulated.

## Resulting design

The generic commitment path may construct an immutable, typed selection request for a candidate whose effects are all recognized built-in Contract declarations. An optional semantic Store reader returns only existing incoming/parent Contracts, Assets selected by the existing input/context rules, allowance facts related to the referenced sources, and exact candidate replay evidence. The request covers existing incoming Contract IDs, direct parents and a claim-bound parent; it does not insert pending declarations into the existing-Contract selection. Asset IDs come only from input bindings for declared input roles and, for task versions v4/v5/v6, the existing context Asset IDs. The reader selects data; canonical parsing, projection rules, authority and rejection remain with their existing owners.

The request is only a read hint. Known declaration-shape or version errors, unsupported effects and Stores without this optional capability use the original full-context path, where the original reducer preserves authentication and audit behavior. Unknown programming errors still propagate. Registered but unused injected handlers do not disable the pure built-in path; a candidate that actually uses an injected effect stays on the full path.

SQLite uses partial expression indexes over the existing runtime-record table for the five allowance relation fields. The change adds no table, summary balance, cache authority or new fact. Reads are split below SQLite's bind-variable limit, accounting for the five fixed relation-type parameters in every query chunk; at the tested limit of 999 variables, 199 source IDs are served across bounded chunks. Existing custom Stores remain compatible through the original full-context path when they do not expose the optional semantic reader. The in-memory Store retains its full-history behavior while applying the same exact selection semantics.

Replay selection includes matching receipts and committed heads, then every member named by each selected head. Duplicate receipt/head matches and duplicate members remain visible to the existing full-head proof; selection does not collapse malformed history or replace replay validation. The writer's allowance checks, authorization revalidation and atomic commit boundary are unchanged.

## Verification and measured scope

The author recheck passed 32 projection/ownership tests and 182 adjacent commitment, replay, reducer, input-binding and Store tests; two Redis-dependent cases were deselected. Ruff check and format check passed. Parameter-limit and malformed in-memory-history regressions were first run against the previous implementation and their failing outputs were retained.

With one fixed candidate and the same exact references, signed closed unrelated history H=0/16/64 returned 2 Contracts, 2 Assets and 6 runtime records at each size. The query materialized 11 SQLite rows, decoded 7 RuntimeRecords and issued 7 SELECT statements at each size. The selected allowance relation counts were 2 grants, 1 reservation, 0 returns and 0 extinguishments. EXPLAIN selected the five relation indexes. For the missing-source selector used in the VM comparison, indexed execution used 102 progress-handler callbacks versus 860 with those indexes removed, an 88.1% reduction. VM callbacks are a database-work proxy, not a row count or wall-time claim.

## Limits

This slice reduces projection-context history reads. It does not close writer-side allowance validation, cumulative same-parent balance work or publication-proof P² behavior. Those remain open for subsequent C work; the measurements here do not establish whole-workflow scaling or installation-level acceptance. Full combined-suite and installed-artifact verification remain separate integration gates.
