# Local Worker fleet and runtime compilation

Contracts now distinguish the capabilities and pools required for their own
execution from the bounded scope available to delegated work. A declarative
local Fleet runs heterogeneous Worker profiles concurrently through independent
SQLite connections and the existing pull/claim/submit protocol.

Parallel provider tool calls compile into independent tool tasks and a boolean
join continuation. Claim-bound structural rejection now preserves signed raw
output and enters the same durable recovery path as projection rejection.
Concurrent output submissions are re-reduced under SQLite's writer lock so an
output-complete task receives exactly one terminal fact.

The AI4S literature example now publishes a Skill, one root task, and a fleet
configuration; planning Workers compile its child graph at runtime.

Tool-execution capabilities are exclusive, and planned/recovery tasks with
remaining tool scope must dispatch tools before publishing outputs. One
unambiguous provider-quoted method action is normalized back to the canonical
action parser; ambiguous output still fails closed.

Independent acceptance can bind a deterministic JSON `output_shapes` policy.
The policy is inherited through planning, recovery, retry, and continuation,
checked at producer submission, and checked again before qualification.
Verifier output and `asset.attest` now share one atomic effect group. A verifier
needed by an independently accepted ancestor remains reachable after an
intermediate planning parent completes.
