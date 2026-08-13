# Verifiable derived evidence and configured tool execution

Slice relations now bind a derivation version and exact range. Slice creation,
CLI/API publication, disclosure policy, and verification recompute the bytes
or characters from the committed source Asset; a signed relation is an
assertion, not proof that arbitrary replacement content is a valid slice.

`aig run --tool-registry FILE_OR_MODULE:FACTORY` explicitly loads trusted
operator code, publishes public tool descriptors through Candidate commitment,
gives the LLM only tools declared by the current Contract, and runs a separate
capability-routed ToolWorker. Multiple provider tool calls fail visibly because
the current protocol action represents one decision.

After a successful tool task, its continuation removes the used tool from its
scope. Repeated calls require separate ordinary tasks, preventing a model from
silently burning the lineage allowance in a same-tool continuation loop.

Independent acceptance may bind an exact output Asset produced by the target
Contract or any immutable descendant. Unrelated same-name Assets remain
ineligible. This permits tool and planning subtasks without weakening the
producer/verifier separation.
