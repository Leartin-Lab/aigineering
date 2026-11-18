# Closed primitive task runtime

## Problem

Task fulfillment had acquired extra controllers, source-tree restrictions and
feature-specific state transitions. A signed extension could emit derived facts;
completion could occur after the originating publication. Those paths enlarged
the trusted surface and split the causal evidence for one action.

## Resulting design

- One action is one signed Candidate, one receipt, one complete publication head
  and one SQLite transaction. The head lists the actual committed members,
  including reducer consequences and claim transitions. Members never point back
  to their own head. Exact replay authenticates the current key and reads this
  original closure without rerunning effect makers.
- The fixed core effect set admits Assets, bounded Contracts, policy attestations
  and identity/claim operations. Arbitrary injected RuntimeRecord makers and
  post-completion controllers are removed. The same pure reducer handles required
  Assets, new declarations and canonical qualification.
- Any admitted Asset meeting a Task's frozen requirements can fulfill it,
  regardless of producer Task, root or origin. Explicit hash, producer, shape
  and independent-policy conditions still apply. Fulfillment cancels
  unfinished descendants; it does not require an output-delivery action.
- Tool work and continuation are ordinary declarations in the originating
  Candidate. Review requests compile to a Task and descriptive Asset; decisions
  compile to canonical `asset.attest` plus a descriptive Asset. Recovery adapters
  publish ordinary signed work only from actual remaining causal funding.
- Failure closes the attempt and consumes the existing allocation, without
  declaring the unmet business obligation permanently failed. Legacy failed
  terminals remain immutable audit history. Later satisfaction and ancestor
  cancellation are derived without writing a second terminal or resetting claims.
- Indexed reads select affected Tasks, required Assets, target financial records
  and actual ancestors. Shared-connection transactions and cursor reads are
  serialized; observations cannot join another thread's transaction.

## Public compatibility

Fresh handoff, edit/revision, assignment, dependency edges, holds/promotion,
priority, comments and attachment-link writers are removed. Historical records
remain inspectable where supported, without current task authority. Ordinary
cancel/retry, Asset publication, review and historical attachment export remain.
Python callers must remove `additional_effects` and completion-registry setup.
New domain methods compile to ordinary primitive Candidates.

Schema 24 fences older readers. Stop older processes and back up before upgrade;
already-open old connections cannot be fenced by an open-time reader check.
Historical facts are not rewritten. Legacy incomplete publication heads cannot
be used to reconstruct a fresh accepted effect set during replay.

This supersedes the retired writer clauses in ADR-033, ADR-035, ADR-036,
ADR-038, ADR-039, ADR-041 through ADR-043 and ADR-045, the injected-effect seams
in ADR-011/ADR-040, and ADR-034's handoff-owned return publication. Their
historical facts and conservation proofs remain readable. ADR-031's review
policy remains, with its custom effects replaced by primitive compilation;
ADR-032 retains exact input bindings while dropping the implicit tree-output
rule. Transactional Worker authentication (ADR-037) and operational logs
(ADR-044) remain current.

Unfunded root retry remains a conservative allowance
restriction; removing a completion gate does not reauthorize a root grant.
The implemented owners are [DESIGN](../DESIGN.md) and the
[boundary invariants](../docs/boundary-invariants.md).

## Verification scope

Acceptance covers signed atomic closure, readonly replay, cross-task matching,
failure and funding proofs, descendant cancellation, isolated concurrency,
reopen/rebuild, current CLI and installed-artifact checks. Deterministic tests
and historical fixtures do not establish real-provider reliability or a stable
release. The integrated deterministic suite and isolated installed CLI checks
passed. This change does not claim real-provider execution or a stable release.
