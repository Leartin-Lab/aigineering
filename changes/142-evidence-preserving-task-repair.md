# Evidence-preserving continuation and repair

Parallel tool continuations retain their parent's declared inputs along with
the new observations, just as single-tool continuations do. Their activation
still waits for the new observations. Historical declaration formats and exact
publication replay remain unchanged.

The LLM adapter checks native function names against the tools advertised in
that exact request before parsing arguments. An unsolicited name produces
`tool_call_not_advertised`, with provider usage retained. Protocol actions are
not silently translated from native function names; an explicitly advertised
function with the same name remains an ordinary tool.

Generic repair separates causal funding from disclosure scope. Its authorized
publisher preserves the failed Task's scope and original description metadata,
and reserves actual available ancestor allowance through ordinary Candidate
admission. A funder does not need the failed Task's derived inputs. Specialized
tool-consumer proof and all current authority checks remain in force. Existing
recovery publications are read from their exact receipt/head before selecting
funds or compiling new work.

Recovery history capture holds one existing SQLite read transaction across all
fact families; concurrent WAL commits cannot split the capture. Publication
starts after the read scope closes and retains its writer-time checks.

Repair still publishes one ordinary Task and its context Asset atomically.
Failed claims and allocation costs remain unchanged; there are no new primitive
effects, tables, task-state controllers, root grants or implicit JSON repairs.
