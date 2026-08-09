# Documentation map

This index routes readers to the one public owner for each kind of truth. It is
navigation, not another architecture specification.

| Question | Public owner |
| --- | --- |
| What does the runtime implement now? | [`DESIGN.md`](../DESIGN.md) |
| Which guarantees may not be weakened? | [`boundary-invariants.md`](boundary-invariants.md) |
| Why was a durable architecture choice made? | [`adr/`](adr/) |
| What bounded design transition is underway or complete? | [`changes/`](../changes/) |
| What is supported by the current release? | [`ROADMAP.md`](../ROADMAP.md) |
| Where is reproducible release evidence? | [`reports/`](../reports/) |
| How do other implementations verify the wire protocol? | [`conformance/`](../conformance/) |
| How do contributors change the project? | [`CONTRIBUTING.md`](../CONTRIBUTING.md) |
| How does an AI coding worker use this repository? | [`SKILL.md`](../SKILL.md) |

## Reading paths

Runtime users should start with the [README](../README.md), then use the
[roadmap](../ROADMAP.md) for supported scope and the
[design](../DESIGN.md) for implemented behavior.

Contributors changing a correctness boundary must read the
[boundary invariants](boundary-invariants.md), the relevant
[architecture decision](adr/), and the active or completed
[change record](../changes/) before changing code. Acceptance claims belong in
[reports](../reports/) only after their commands and evidence are reproducible.

The [whitepaper](whitepaper.md) explains the model at a conceptual level. It
does not override the implemented design, invariants, or release scope.

## Ownership rules

- Current behavior is updated in `DESIGN.md`, not inferred from historical ADRs.
- ADRs record durable decisions and consequences, not implementation progress.
- Change records define migration and closure for one bounded transition.
- Reports preserve important acceptance evidence, not exploratory reasoning.
- Stable reference material gets a separate document only when it has an
  independent reader and executable verification.
- Private review notes, analogies, credentials, and speculative work never
  enter public release artifacts.
