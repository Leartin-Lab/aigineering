# Change 008: Causal allowance as the single budget source

Status: Implemented and verified on dev; release pending
Target: v0.5.3
Decision: `docs/adr/ADR-013-causal-allowance-facts.md`

## Problem

Task-completion projection reconstructs a process-local `BudgetManager` from
`Contract.budget`. Causal allowance facts already own grants, reservations,
returns, and terminal extinguishment. The local table can therefore disagree
with the durable ledger after child publication or restart.

## Intended change

- completion, recovery, continuation, and trace summaries read remaining work
  allowance from immutable allowance facts;
- legacy Contracts without allowance facts fall back to their declared budget;
- publishing new tasks no longer mutates a process-local budget table;
- the unused `BudgetManager` implementation is removed.

## Non-goals

- changing allowance accounting or task prices;
- adding Worker accounts or transferable balances;
- changing Contract wire identity;
- introducing a cache into commitment decisions.

## Verification and exit criteria

Completion and recovery behavior must remain equal for legacy fixtures, while
reserved and extinguished allowance is reported exactly after restart. Full
planning, recovery, reconstruction, trace, and release gates must pass with no
production import of `BudgetManager`.

## Implementation evidence

- completion, recovery, continuation, and terminal trace construction resolve
  the immutable allowance ledger with an explicit legacy fallback;
- task failure-risk projection reuses `RuntimeProjection.budget_remaining`
  instead of replaying mutable-looking trace counters;
- Candidate publication no longer initializes process-local budget entries;
- the `BudgetManager` module and all production imports were removed and an
  architecture test prevents reintroduction;
- restart-equivalent completion contexts report the same post-reservation
  balance;
- Ruff and the full deterministic suite passed with 1126 tests and 3 skips.
