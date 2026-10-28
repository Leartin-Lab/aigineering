# Default allowance for planned tool tasks

Status: Implemented development increment
Date: 2026-10-23

Planning expansion now defaults an omitted child `budget` to 2 when that child
declares a non-empty `tool_scope`, covering one tool task and its continuation.
Children without tools still default to 1. Legacy `contracts` blueprints and
structured planning scaffolds use the same rule. A present budget value keeps
the existing parsing and validation behavior: explicit 1 remains rejected for
tool tasks, and malformed values are not upgraded to the tool default.

The existing parent allowance remains the limit. A tool task is rejected when
less than 2 units are fundable, including after earlier siblings consume the
remaining allowance. Explicit larger requests continue through existing budget
containment and are never increased. Planning compile prompts now state the
minimum for tool tasks and require explicit allowance for further tools or
planning stages, based on the actual work rather than a universal formula.
Funding-limit rejections preserve the existing `requested`, `effective`, and
`remaining` diagnostic fields so callers retain the same actionable budget
evidence.

Planning remains an ordinary runtime flow: `/plan` publishes the existing
planning stages and compile proposes ordinary contained child Contracts. This
change adds no scheduler, domain DAG, continuation input authority, or recovery
policy. Verification covers both blueprint formats, omitted and explicit
budgets, parent allowance limits, and the public compile prompt.
