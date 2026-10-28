# Repository workflow

This is the public contribution contract. Use it with
[CONTRIBUTING](../CONTRIBUTING.md), [DESIGN](../DESIGN.md),
[boundary invariants](boundary-invariants.md) and relevant public ADRs.

- Define one bounded requirement, the affected owners, dependencies and observable
  acceptance behavior. Recheck historical findings against current code.
- Obtain independent plan review before implementation. Develop and verify small
  slices; review the exact resulting snapshot independently of its author.
- Keep ordinary implementation autonomous within the agreed scope. Return defects
  to implementation; changes to scope or standards require the appropriate decision.
- Integrate real dependencies and review the combined result. Author self-checks,
  independent technical acceptance and the user's product acceptance are separate.
- Record commands, artifacts, results and limitations against an exact snapshot.
  Keep credentials, private inputs, raw Stores and development conversations out
  of public documents and distributions.
- Public reports are selected cases with inputs, procedure, outcome and limits.
  Internal plans and diagnostic ledgers are not user documentation.
- Commit, push, merge, tag and publish only within current authorization. A local
  commit or development-branch update is not a stable release.

One semantic owner per rule, explicit boundary validation, correlated failure
evidence and behavior checks through stable interfaces remain the technical
baseline. Do not add a runtime scheduler, authority source or domain mechanism
to support a contribution workflow.
