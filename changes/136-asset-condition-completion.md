# Asset-condition completion and shared Definitions

Status: Implemented and independently reviewed development change.

Contracts may select output Assets by declared role name and optional exact
Definition ID, Content ID, producer actor IDs, and output shape. Without explicit
selectors, matching same-name business output remains the compatibility rule.
Any admitted Asset meeting those conditions can satisfy the receiving Contract,
without a delivery action, producer-tree requirement, or target claim. Tool and
MCP observations cannot satisfy ordinary business roles by name. Explicitly
declared protected observation roles remain eligible subject to publication
authority. Independent acceptance still requires exact qualification.

When a matching Asset predates a Contract, declaration-time completion is
projected through the existing Candidate writer and reducer. The same output
conditions drive pending and stored Assets; historical Asset bytes and typed
terminal facts are not rewritten. A control-plane Candidate retains its
operation-specific authority but cannot force completion.

Claim-bound `/exec` may reuse an already frozen Definition. The signed output
assertion binds the authenticated Candidate actor and key to its exact claimed
Contract and output role; Definition `source_uri` is provenance metadata, not
producer proof. Engine roots publish Definitions for the frozen goal and exact
mapped inputs. Unknown but syntactically valid required Definitions remain
unmet until their signed graph facts exist; they do not fall back to name
matching. Existing signed bridge history is preserved and may require new
ordinary work if it does not meet a new goal's conditions.

The SQLite reader floor is schema23. No table or column is added. Historical
Contract policies and terminals remain unchanged; successor publication filters
conditions to its retained output roles. Recursive recovery has no fixed
three-level cutoff; existing signed source-history and causal-funding rules
remain. Ordinary completion cancels unfinished descendants without guessing a
verifier obligation from capabilities or input names. Staged compilers retain
their parent's independent policy instead.

Fresh qualification facts are rechecked against canonical attestation semantics
under the writer lock. Schema23 does not reauthenticate or repair previously
committed qualifications or terminal facts. If an older custom extension
published noncanonical qualifications, upgrade preserves them; use a new
workspace and new publication/acceptance evidence for the corrected boundary.

Validation covers deterministic signed ingress, concurrency, exact replay,
reopen/rebuild, and installed CLI/package behavior. This does not establish
live-provider reliability, broad performance, or stable-release qualification.

This record supersedes the output-source and producer-tree defaults described
in changes 134 and 135; those records remain unchanged as history.
