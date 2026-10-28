# Local Worker operational logs

The task logs command reads a bounded, store-adjacent capture for one exact
signed local Worker claim. The store option selects the SQLite Store and its
adjacent worker-logs spool; pagination cursors bind the Store root, domain,
task, claim, capture, byte boundary and verified event prefix. Follow polls the
same capture until its timeout or local interruption and does not cancel
execution.

The quick, pool, target and configured/Fleet local invocation paths capture
typed lifecycle events by default. The LLM adapter emits only classified
provider status, retry count and delay at its actual retry branches. Captures
exclude prompts, credentials, headers, responses, raw Worker output, tool
payloads and arbitrary exception text. Custom Worker stdout/stderr and full
transcripts are not captured.

Captures are auxiliary local files, not signed facts, task state or business
Assets. They do not establish claim authority, Candidate acceptance, output
qualification or task completion. Readers reuse the task diagnostics claim
proof and require Redis; SQLite remains the authority. Capture write failures
are best effort and are reported separately from Worker errors. Missing files
do not disclose why capture failed.

Files are exclusive-create, bounded, private and hash-chained for accidental
corruption detection. The chain is not an authentication mechanism. Partial,
corrupt, incomplete and truncated captures remain distinguishable. A valid
prefix without an invocation-finished event is incomplete, not proof that the
Worker completed. Automatic retention, pruning, remote capture, arbitrary log
sources and transcript export remain outside this change.

Focused validation uses deterministic Workers and fake transport retry events.
It is not real-provider evidence or stable-release qualification.
