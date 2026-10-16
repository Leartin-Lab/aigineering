# Change 099: Tool and MCP adapter failure convergence

Tool and synchronous MCP adapter preflight and invocation failures now raise
the existing `WorkerExecutionError` with a stable safe code and fixed message.
Arbitrary exception text, arguments, and results do not become observation
Candidates or durable diagnostics. The ordinary Worker invocation-failure path
closes the claim and exposes the safe cause through task runs and claim-bound
diagnostics. Recovery, when available, is new ordinary work and may not have an
executable target. Successful observation wire shapes
and metadata remain compatible; negative business JSON returned by a handler
is still an `ok:true` observation whose result is the original string.

Single-tool completion now publishes the ordinary continuation for committed
legacy observations regardless of their historical outer `ok` value. This is
forward compatibility for immutable local evidence, not proof of tool success
or business qualification. Completion checks the exact parent for an existing
typed terminal before publishing completion consequences and preserves it.
Parallel joins continue to require committed observations; new adapter failures
produce none.

The MCP adapter remains a synchronous callable interface with object arguments,
nonempty tool names, descriptor and scope checks, and string results. A missing
server is distinguished from a `KeyError` raised inside a callable. This change
adds no transport, timeout isolation, or new runtime lifecycle.

Verification: focused adapter, signed Tool and MCP failures, legacy completion,
parallel join, Worker architecture, and documentation governance checks. Full
integration and development delivery await combined independent review.
