# Atomic local Asset publication

Slice creation previously published its Asset before publishing the relation
that binds its source and range. A rejected or interrupted second publication
could leave the slice and its fulfillment consequences without that relation.
Demo and quick GOAL setup also published configuration, inputs and root in
separate actions.

Slice ingress now verifies the proposed derivation and publishes the Asset and
relation in one signed Candidate. Existing exact bytes, lineage and disclosure
are preserved. Demo and quick GOAL submit their setup effects as one batch.
Ordinary later Worker actions and domain bootstrap remain separate actions.

An `asset.relate` effect may use `replacement_effect_index` instead of
`claim.replacement_asset_id`. The index must be an integer (not a boolean)
identifying a preceding `asset.propose` in the same signed Candidate. The
pure batch compiler resolves that proposal's already projected Asset ID.
Both endpoint forms together, invalid indices and non-proposal targets are
rejected with the whole Candidate. Durable relation facts retain only exact
Asset IDs. Exact replay reads the original publication without resolving again.
This does not add name resolution, dynamic references, a new effect type,
state controller, table or scheduler.

Relation signatures remain assertions: existing exact-ID relations do not gain
universal semantic validation. Slice ingress uses the existing derivation
verifier before admission, and disclosure policies still verify the admitted
exact relation under ADR-018.

Configured-run resource/root setup still uses separate publications and remains
an identified follow-up defect. This change does not claim all composite CLI
publication paths are repaired, real-provider qualification or a stable release.
