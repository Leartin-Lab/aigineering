# Boundary invariants

These invariants describe the current signed task runtime. Aigineering is a
single-machine system: SQLite is the durable fact and claim authority, while
Redis provides required application discovery, event feeds and Worker wakeups.

## 1. State changes use one signed Candidate

Every state-changing publication is an actor-signed Candidate. Projection is
pure. The authenticated Candidate is checked against current authority,
references and causal allowance, then its complete effect set commits in one
SQLite transaction. Accepted facts, trace, idempotency and claim transitions
cannot be split across follow-up writes. A failed transaction leaves none of
that Candidate's effects committed.

## 2. Replay reads the exact accepted publication

Replay is read-only: it verifies the original signed Candidate, current key
authorization, receipt, head and effect membership. It returns the accepted
publication; it does not rerun an effect handler, reconstruct a replacement
Candidate or write a marker. A new intent is a new Candidate under current
authorization.

## 3. Any admitted matching Asset can meet an output

Task completion follows ordinary output requirements. An admitted Asset can
meet a required name and any explicitly frozen definition, content, producer
actor and shape selectors, regardless of its source Task or origin. An
independent acceptance policy adds its own exact qualification requirement.
Root identifiers and historical tree or origin labels do not create a second
completion rule. Root status is an observation of its declared requirements.

Protected namespaces still require their explicit publishing authority.
Tool observations remain local evidence and cannot impersonate business
outputs. Only the canonical signed `asset.attest` effect creates qualification
facts; an ordinary relation Asset can still satisfy an output when ordinary
frozen requirements match it.

## 4. Workers publish ordinary bounded work

Planning and delegation produce ordinary Contracts within parent scope and
causal funding. A single-tool action declares the tool task and its continuation
in the same Candidate. Tool scope permits a call; it does not require one.
Observations do not become business output merely because a tool ran.

There is no post-completion controller or execution DAG. Runtime views inspect facts and cannot create work or mark a task complete.
Authorized recovery adapters publish ordinary signed Candidates through admission.

## 5. Attempts, failures and funding stay distinct

Claims, attempt outcomes and failure audits are immutable observations. A
failed invocation is not itself a permanent business failure of the Contract.
New work requires a real available grant within the causal funding chain;
recovery cannot fabricate a root grant or reuse extinguished allowance. An
unmet output with no eligible funded work remains an observable blocker. This
failure and recovery behavior is covered by bounded deterministic and installed
CLI checks.

## 6. Independent review is ordinary publication

A review request is an ordinary Task and descriptive Asset. A decision commits
the canonical `asset.attest` effect and its descriptive decision Asset in one
Candidate. Reports and targets are ordinary admitted Assets checked against
the frozen output selectors, shape and producer-separation rule. Review
relations do not emit qualification facts by themselves.

## 7. Claims and current authority fence execution

Operational claim and renewal commands carry the original signed Candidate.
The writer transaction rechecks the effective key, capability, enabled Worker
registration, exact command and current claim state. Claim history cannot be
reset by views, retry, recovery or replay. Causal allowance reserves actual
work and cannot be minted by declaring a tool or recovery scope.

## 8. Views and diagnostics do not write task state

Task status, audit, lineage, diagnostics, logs and rebuilds are read-only
projections of committed facts. They cannot qualify output, repair history,
assign work, create a recovery publication or change claim eligibility.
Unverified or missing evidence remains visible as such.

## 9. Disclosure and capability are separate

Exact input bindings and disclosure policy determine what context a Worker can
see. Capability routing determines which Worker may execute. Skills and labels
resolve context; they do not grant authority. Sealed values are not placed in
prompts or exported views.

## 10. Schema upgrades fence new readers

Schema 24 is the reader floor for the current output and Candidate semantics.
Stop older Workers and back up before upgrading. The floor prevents an older
process from opening the upgraded Store; it cannot constrain an older
connection that was already open. SQLite remains authoritative if Redis relay
synchronization is pending.
