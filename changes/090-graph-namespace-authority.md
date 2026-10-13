# Graph namespace authority

The Candidate batch projector now applies reserved-name authority to signed
asset-graph definitions and to Assets materialized by new graph assertions.
Publication requires the current actor's `asset.publish.protected` capability,
or an exact name listed in the validated claimed Contract's
`minting_authority`. The assertion check also covers a historical definition
that the new assertion reuses. Ordinary graph names and existing
`asset.propose` authorization continue to use their existing rules.

The guard applies to new projections and does not rewrite historical committed
facts during rebuild. Stop older Worker processes before relying on this boundary
so output is submitted through the updated runtime.

Validation: focused signed Candidate graph tests cover protected-capability
publication, atomic direct rejection, historical-definition reuse, exact
claimed-output delegation, and replay plus strict rebuild/reopen. Rejections
leave business graph facts unchanged while persisting the audit and trace.
