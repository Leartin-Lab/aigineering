# Provider output diagnostics and planning-stage constraints

An OpenAI-compatible completion that reports `finish_reason="length"` now fails
with the stable Worker error code `output_truncated`, even when its partial
content parses as an action. A completion with empty or whitespace-only message
content fails with `empty_response`. Both failures retain validated provider
usage for the runtime's existing durable failure path. Reasoning-only fields are
never treated as executable output. Responses without a finish reason retain
the existing compatibility behavior.

Planning and replanning stage prompts distinguish the current stage action from
an ancestor's quoted first-action instruction. A stage completes its own
declared `/exec` output instead of repeating an ancestor's already-issued
`/plan` or `/replan`; this scoped clarification preserves new planning when the
current stage independently needs more information.

Planning and replanning compilers preserve explicit current-stage child
requirements from their draft and dependency analysis, including allowance
budgets. The plan compiler declares only direct children; any future replan
successors remain under the child's later replan compiler. This does not limit
independent direct children. Stage prompts also clarify the two action-wire
representations: slash actions encode output content as a JSON string, while
provider JSON-action mode passes an output object or array for one canonical
serialization by the adapter.
