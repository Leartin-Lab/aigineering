# Change125: Preserve output policy for pending Asset facts

## Problem

FactReducer used the names of every pending Asset as an output-completion shortcut. A signed batch with a valid business output and a tool/MCP/label-placeholder observation could complete an ordinary Contract despite its declared outputs failing the canonical stored-output predicate. The same shortcut could let an unfinished child evade the parent's cancellation fence. The defect existed before the Asset context optimization.

## Resulting behavior

Completion now evaluates actual pending Assets through the shared output-satisfaction policy. Tool, MCP and label-placeholder observations cannot satisfy ordinary business outputs by name alone; system and recovery outputs require their own Contract as producer; independent acceptance still requires exact qualification. Activation continues using pending names, and all same-name values are individually checked. The pure reducer supplies tuples; the shared policy remains the single eligibility owner.

Legacy explicit name-only helper callers retain their existing API behavior. No database, schema/index, authority, acceptance, claim or scheduler is added. Historical completion facts are immutable and are not repaired by audit/rebuild. Run corrected code on every writer process to obtain the new behavior; already-open old versions retain their own logic.

## Verification and limits

Independent49 focused tests, four actual signed publication vectors, five-file Ruff/format, and an old-code-created bad terminal reopened/rebuilt without changing its facts/digest passed. Exact snapshots and failure classification are in the [plan](../docs/plans/pending-asset-output-policy.md). Root combination and separate overall gates are tracked in [report113](../reports/113-asset-context-direct-transport-development-2026-10-25.md). This is a correctness repair, not Asset locality or stable-release qualification.
