# Task attempt history and diagnostics

`aig task runs CONTRACT_ID` lists the exact task's immutable claim allocation,
effective lease, and proven outcome. `aig task diagnostics CONTRACT_ID --claim
CLAIM_ID` shows safe summaries of the exact claim's causal runtime facts. Both
commands support bounded cursor pages and JSON output. Their history comes from
SQLite facts at a fixed revision; they use the required Redis collaboration
preflight and report Redis unavailability explicitly.

Claim allocation is not provider execution. The commands distinguish submitted
output, authenticated rejection, invocation failure, durable expiry, release,
and a lease that has elapsed without an expiry fact. Exact receipt, committed
head, rejection, attempt closure, and release parents are checked. Missing or
conflicting proof is shown as an integrity issue and makes the command exit
nonzero. The current Store allows one claim per immutable Contract; retries,
recovery, and edits create successor Contracts that are inspected by their own
IDs.

Rows expose a Worker ID only when an immutable registration declares it, and a
package ID only when it matches the canonical `pkg:<64 lowercase hex>` package
identity. A `null` package ID means the stored value is legacy or otherwise
noncanonical; it does not mean the claim had no package. Causal checks still
compare the original stored values exactly.

Candidate IDs are shown only in their canonical `candidate:v1:<64 lowercase
hex>` form. Malformed values remain available to exact internal integrity checks
but appear as `null` in the run summary. Runtime-record timestamps are shown only
when they parse as UTC; malformed timestamps appear as `null` and produce a
stable integrity issue. A release linked to a terminal fact is accepted only
when that fact is valid and belongs to the same Contract with an owner-shaped
causal witness.

Output omits raw Worker output, arbitrary exception text, prompts, trace bodies,
credentials, and unrecognized error categories. Unknown usage is left unknown;
reported token counts are metadata, not billing. These commands inspect
execution facts and do not capture or replay Worker transcripts. The existing
SQLite facts remain authoritative, and inspection does not repair projections
or publish task completion.
