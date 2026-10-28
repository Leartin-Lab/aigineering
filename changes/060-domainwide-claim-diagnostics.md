# Domain-wide claim provenance in task diagnostics

Task attempt diagnostics now recognize the existing signed `worker.claim`
command's nullable Contract selector. An explicit `contract_id: null` means
the Worker requested an eligible task from the domain-wide pool; the resulting
grant still identifies one exact Contract. A targeted claim remains valid only
when its signed selector exactly matches that grant's Contract.

The read owner follows the grant's sole request parent and that request's sole
Candidate receipt parent. It checks the canonical request payload, exact Worker
and key identity, Candidate/receipt correspondence, runtime-record identities,
and receipt → request → grant revision order. Missing selectors, malformed
selectors, a different pinned Contract, or mismatched causal evidence remain
integrity errors. This view does not authenticate or publish claims, scan by
Candidate ID, or change claim facts, selection, or authority.

Malformed historical receipt effect lists and committed-member containers are
classified as integrity errors rather than raising while producing the read
view. Their raw values are not echoed or treated as provenance.

Terminal state and attempt outcome values are also type-checked before enum
membership checks, so malformed historical containers cannot crash selected
claim diagnostics.

The correction addresses a task-diagnostics false rejection observed after one
real `run --once` produced an accepted Worker output. That earlier attempt's
facts and failed inspection evidence remain unchanged; this change does not
retroactively mark that run or its whole report workflow successful.
