# Scientific table profile contract

`data_profile` has schema version `scientific-tabular-profile-v1` and contains:

- `input_sha256` and `size_bytes` for exact provenance;
- `format`, `delimiter`, `encoding`, `missing_tokens`, and `field_names_revealed`;
- `scanned_rows`, `column_count`, and `truncated`;
- one column record with a stable position/token, non-missing and missing
  counts, distinct count within the scanned scope, and a conservative inferred
  scalar type. `distinct_count_truncated` marks the bounded 10,000-value
  cardinality counter; in that case the count is a lower bound.

The type set is `empty`, `boolean`, `integer`, `number`, `text`, or `mixed`.
Inference is syntactic and does not establish scientific type, measurement
scale, identifier status, units, permissible range, or missingness mechanism.

Before planning an analysis, attach a separately reviewed `data_manifest`
containing the observational unit, entity and group identifiers, variable
definitions and units, provenance, inclusion rules, known missing codes,
repeated-measure structure, and any fixed data splits.

The profile never contains raw cell values. Revealing field names is an
explicit local choice and may still expose sensitive metadata.
