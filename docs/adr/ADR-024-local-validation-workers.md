# ADR-024: Operator-configured local validation Workers

Status: Accepted
Date: 2026-09-13
Related: ADR-019, ADR-020, ADR-023

## Context

Real-provider claim review in report 063 completed independent attestation while
missing original-text substitutions and citations. A prompt-only verifier does
not make exact comparison deterministic. Tool observations also cannot stand in
for accepted business outputs, and a tool argument assembled by a model need
not equal the original disclosed Asset.

## Decision

The Fleet launcher accepts an explicit `kind = "local"` profile with a
`worker_factory = "module:factory"` reference. Parsing does not import code;
building the profile imports the operator-installed module and calls its factory
with `worker_id`. The returned Worker must match that identity and implement
`invoke`. The wrapper registers only the profile's configured capabilities,
pools, capacity, version and profile ID; factory-provided registration metadata
is ignored. Effect authority is still granted separately by the existing host.

A local factory is trusted operator code with the process's privileges, not a
sandbox. It is never imported from a method package, Asset, model output or remote
URL. Method import neither installs nor authorizes executable code. Dedicated
Workers should use explicit task capabilities and pools; local Workers have no
new exclusive execution category in kernel routing.

The first business adapters implement exact original-text comparison and explicit
citation binding checks. Method invocations consume exact input IDs from the
existing method binding and emit ordinary `/exec` assessment Candidates. A
separate structural gate reads declared source/report Assets and emits `/fail`
when either check fails. Its success receipt names the exact input IDs and says
`semantic_checked = false`; it never attests the report's semantic correctness.

The citation format adds `evidence_bindings`, a list of `{excerpt_id, quote}`
records per claim. Quoted text must occur literally in the named source excerpt,
and binding IDs must equal the declared citation-ID set. Duplicate identical
bindings, unknown IDs and incomplete declarations fail. Multiple distinct quotes
from one excerpt are allowed. This proves consistency of explicit references,
not whether the surrounding argument uses other, undeclared evidence.

## Consequences

- The existing method test/assess/independent-review/reuse path works with real
  deterministic implementations without a provider or a private orchestration loop.
- A completed assessment containing `passed = false` is an observable finding;
  callers needing a blocking task use the structural gate's terminal failure.
- Malformed, ambiguous, unavailable or substituted inputs fail closed. Source
  text is compared without Unicode normalization. Input sizes are bounded.
- Correct quotations can still be irrelevant or insufficient. Causal inference,
  verdict choice, probability claims and missing semantic context remain outside
  these checks. Historical reports without bindings fail the new declaration
  format; that does not prove automatic discovery of their implicit citation gap.
- No core effect, Store schema, commitment rule or scheduler is added.
