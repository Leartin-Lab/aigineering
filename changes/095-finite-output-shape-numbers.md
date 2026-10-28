# Change 095: Reject non-finite numbers in declared output shapes

Declared deterministic output shapes now reject non-standard JSON numeric
constants and non-finite parsed numbers, including exponent overflow. Finite
integers and floats remain valid numbers; booleans remain distinct. Outputs
without a declared shape remain ordinary text.

The existing acceptance owner enforces the same check during claim-bound output
submission and independent qualification. Historical committed facts are not
rewritten: the check constrains new output submissions and new qualification
attempts. No schema, protocol, or new acceptance owner was added.

Validation: focused independent-acceptance checks, Ruff, formatting, and diff
checks passed.
