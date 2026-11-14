# Atomic configured publication

Configured launch previously published its input and Skill Assets before
validating and publishing the exact-bound Contract. A later failure could
leave those Assets and their completion consequences without the intended
Contract. Setup now compiles and submits one signed Candidate containing
both resources and the declaration. Its idempotency key binds the entire
setup intent, including resource contents and metadata.

The existing `contract.declare` primitive can opt into `bind_local_assets: true`.
In that format, `candidate-asset:N` selects a preceding `asset.propose` in the
same signed batch, only in active `input_asset_bindings`, `context_asset_ids`
and `acceptance_policy.evidence_asset_ids`. The index is canonical non-negative
integer text. Missing, self, future or non-proposal targets reject the complete
publication. At least one active input must contain a local reference, so an
older writer cannot silently accept the template while ignoring the marker:
its existing exact-input check rejects the missing Asset.

The template's canonical identity is validated before compilation. The result
uses the existing canonical identity owner and admission rules, with actual
Asset IDs and an actual Contract ID returned by commitment. Ordinary declarations
are unchanged; literal prose and inactive inherited input bindings are not
rewritten. Claim-bound mixed output/declaration actions remain disallowed.

Read hints share the scalar identity calculation without invoking a projector
or maker. One batch caches repeated scalar references locally; it does not
store an execution graph or query unrelated history. Admission uses the actual
already projected Assets. Replay authenticates and reads the original complete
closure without compiling again. Asset identity, exact content, disclosure,
qualification and causal funding retain their existing rules. No new effect
type, table, scheduler or Task state controller is introduced.

The configured entrypoint also drops its historical output-name and Task-name
creation guard. Existing Assets satisfy only the Contract's ordinary explicit
requirements; a display name is not a source or ownership boundary. Identical
setup replays the same publication. Changed input, Skill or requirements can
create a distinct obligation with the same display name. `--resume` still
selects an exact existing Contract without rereading its original resources.
Freshness requirements belong in ordinary frozen selectors and policy.

Rejected or interrupted setup commits no partial resource/declaration or
completion/cancellation consequences. Identity bootstrap, later Worker
registration and execution remain distinct actions. Deterministic, failure,
replay/rebuild and installed-artifact verification are separate from general
real-provider reliability and stable-release qualification.
