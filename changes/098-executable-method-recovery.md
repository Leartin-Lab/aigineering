# Change 098: Preserve executable method intent in recovery

When an admitted system tool method fails, expires or has its submitted
Candidate rejected, recovery can publish a signed system successor that keeps
the original tool name and arguments, inputs, outputs, routing scope,
capabilities and policy. The successor runs through the ordinary Worker claim,
Candidate publisher and existing tool-completion/continuation owners. Exact
claim-run evidence must precede the accepted recovery publication; read-only
lineage verifies the same source and publication history.
The new method context records typed source bindings without copying raw
Worker exception text or rejected output into the prompt.

Recovery history checks both the frozen legacy repair intent and the new
method-preserving intent from one immutable capture. Conflicting history or an
ambiguous dual match does not schedule new work. An accepted legacy methodless
publication remains unavailable for replay as a deterministic tool call; the
recovery-unavailable outcome retains that exact legacy Contract ID for audit.
Read-only diagnostics expose only a fixed safe reason and label any reported
target as an unverified observation; unavailable outcomes do not create lineage
edges or establish that the reported target is runnable.
Every recursive successor is proved against its exact direct predecessor's
rederived failure binding and its own complete deterministic publication; a
marker or context Asset alone is not authority. Source bindings are taken from
the same immutable claim/source history and the same accepted publication head.
Ordinary non-method repair continues to use its existing contract, input
selection and recovery-outcome wire shape, even when unrelated JSON happens to
use the reserved marker key names. Missing or ambiguous historical source proof
is an observable safe diagnostic and does not write recovery or terminal facts.

This change does not add a scheduler, task kind, registry, authority, allowance
or provider behavior. Verification uses signed local fixtures and explicit
test-only Workers; it is not evidence of external-provider or production tool
execution.
