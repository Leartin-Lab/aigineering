# Change 134: Output provenance and local workspace delegation

Status: Implemented on dev; stable qualification remains separate.

New application Contracts default to mechanical `output_source = "contract_tree"`.
Completion and successor output retention share one exact-Asset eligibility
rule. Historical Contracts without that field retain their original semantics;
independent acceptance still requires its original exact qualification evidence.
Schema22 raises the reader floor without adding tables or columns. Stop older
Workers and back up before upgrading; already-open old connections are not fenced
by a reopen check.

Engine Workers give each operation a distinct inner root and remap disclosed
input IDs to signed receiving-domain imports. Persistent inner Stores cannot
fulfill a new goal with an unrelated same-name output. Explicit `kind = "engine"`
Fleet profiles can also target an already initialized local workspace, pinned
by directory, Genesis, receiver key and Fleet configuration. They reuse canonical
registration and runtime execution. This requires one trusted operator and
does not provide network federation, a shared budget or process isolation.
See the [configuration recipe](../docs/reference/configured-runs.md#delegate-to-an-existing-local-workspace).

Worker prompts distinguish `/exec` from tools and no longer fabricate a complete
planning blueprint labeled valid for the current Contract. Budget-shortfall
instructions use the existing structured `/fail` action. Capability blockers
point to Fleet routing configuration and distinguish provider request options
from Worker routing requirements.

Deterministic checks cover legacy policies, source ancestry, reader rejection,
exact reopen, distinct operations, input mapping and local receiver fences.
They do not establish model reliability or history-independent query cost.
