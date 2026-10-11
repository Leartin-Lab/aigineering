# Reassignment request recovery

`aig task reassign ... --current` resolves every selected Contract's exact
current assignment head from one immutable snapshot, saves the complete
request locally, and then runs the existing signed reassignment publisher for
each target. Use `--resume REQUEST_ID` to retry the same frozen intent after an
interruption. Resume does not refresh assignment heads or accept intent
overrides; current authorization and writer-time assignment fences still
apply.

Requests are canonical, content-addressed input files under the workspace's
`.aig/requests/reassign/` directory. They bind the resolved Store path, Genesis
domain, observed revision, ordered targets, exact previous heads, action,
Worker, reason, and replay-key base. Files are private and immutable; a request
is not a signed fact, authority grant, execution record, or queue. Human mode
prints a shell-quoted resume command to standard error before target
publication; JSON results include the request ID and the exact requested head
as input context.

Each target remains independently atomic through the existing assignment
owner. Exact committed members replay their original facts under current
authorization, while stale unattempted targets retain the normal writer
rejection. Known partial outcomes and accepted Redis-pending results use the
existing batch collector. A saved request does not provide authorization or
promise automatic recovery after workspace relocation or coordinated local
replacement.
