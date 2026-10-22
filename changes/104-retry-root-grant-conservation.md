# Reject unfunded retry root grants

Status: Implemented development increment
Date: 2026-10-23

CLI retry preserves its source's containment parent and budget. Retrying a root
previously created another root grant without a funding debit. New root
Contracts with `origin="retry"` now fail pure allowance projection. The shared
commit-time allowance validator also refuses new grants of this shape after
canonical declaration and receipt/head/member validation, inside the existing
SQLite writer transaction. Rejection is observable through the existing
`retry_rejected` result and retained audit, with no successor, allowance or
lifecycle half-commit.

Contained CLI retries still reserve their full budget from the common
containment parent; insufficient or terminal funding refuses new publication.
Worker `/retry` remains contained under the claimed source. Candidate identity,
signed effects, deterministic successor IDs and result fields are unchanged.
Historical root retries remain readable, rebuildable and exactly replayable;
the fix neither invalidates nor retroactively removes committed grants.

Behavior coverage includes both CLI spellings, bulk partial results, rejected
relay-pending recovery, insufficient and zero balances, accepted replay after
terminal funding and restart/rebuild, and two SQLite connections competing after
the same preflight balance. A canonical old-style batch passes generic provenance
validation before the writer refusal is tested. Architecture coverage exercises
generic signed publication with both MemoryStore and SQLite, outside CLI routing.

This is the root-grant safety slice of retry funding conservation. It does not
define an original-work authorization domain, choose cancellation/transfer/
freezing of source funds, add dynamic retry budgets, or implement discovery of
old operations across changed actor/key/domain/effect contexts. Existing parent
reservation behavior does not resolve those policy questions. No new allowance
fact type, payload schema or deployment mechanism is introduced. Older code can
still reproduce the defect; this change does not enforce version admission.
