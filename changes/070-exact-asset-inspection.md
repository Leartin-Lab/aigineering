# Exact Asset inspection

`aig asset show` accepts either its existing name argument or `--id ASSET_ID`.
The ID form selects one exact Asset through the existing query projection and
never falls back to a same-name version. Supplying neither selector or both is
rejected before Store setup. The name form and both human/JSON renderers retain
their existing behavior, including local inspection of stored content.

Focused CLI tests cover same-name versions, missing-ID refusal, selector
validation before Store access, unchanged fact/materialization state, and
non-promptable local content inspection. This read-only CLI addition does not
change task disclosure, qualification, signed mutation, or Redis authority.
