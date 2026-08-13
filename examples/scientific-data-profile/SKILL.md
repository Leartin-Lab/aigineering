---
name: scientific-data-profile
description: Safely profile authorized scientific CSV or TSV data through replayable Aigineering tasks. Use before exploratory analysis, model design, or migrating a data-processing agent workflow when schema, missingness, type consistency, leakage risks, and exact input provenance must be checked without exposing raw rows.
---

# Scientific data profile

Use a deterministic script Worker to inspect the physical table, then give an
LLM only the bounded profile needed to design later work. The script does not
infer scientific meaning, clean data, or certify fitness for analysis.

## Publish a narrow task graph

Use ordinary tasks and Assets:

| Task | Inputs | Output | Purpose |
|---|---|---|---|
| manifest | authorized file | `data_manifest` | bind file hash and data dictionary |
| profile | manifest | `data_profile` | bounded schema and missingness |
| design | manifest + profile | `analysis_plan` | units, splits, confounds, methods |
| execute | plan + authorized data | `analysis_result` | run exact transformations/analysis |
| verify | plan + result | attestation or correction | reproduce before claiming |

Do not ask an LLM to inspect raw rows when a deterministic Worker can produce
the required aggregate. Keep confirmatory and exploratory work as separate
tasks. Bind independently reviewed results to an acceptance policy.

## Profile the included fixture

```bash
python scripts/tabular_profile.py assets/measurements.csv \
  --root assets --missing-token NA --action
```

The single `/exec` action publishes `data_profile` and can be passed to
`HarnessCandidateAdapter.result_candidate`. For real data, copy or mount an
authorized file under a dedicated root and change both paths. Never widen the
root merely to bypass a failed path check.

Read [references/profile-contract.md](references/profile-contract.md) before
designing a downstream analysis Worker.

## Preserve the data boundary

- Keep raw data read-only and derived artifacts separate.
- Reject URLs, absolute paths, traversal, symlinks, hard links, special files,
  unsupported formats, oversized files, and malformed rows.
- Bound rows, columns, fields, and output size. Report truncation explicitly.
- Hide field names by default and never emit cell values or direct identifiers.
- Distinguish empty strings from explicit missing codes. Never impute,
  normalize, delete outliers, or reinterpret zero automatically.
- Record the exact input SHA-256, byte count, delimiter, scanned row count,
  missing tokens, and script schema version.

## Require scientific context before analysis

The profile is insufficient without a human-reviewed data dictionary,
observational unit, subject/sample hierarchy, units, pairing or repeated
measures, batches, time structure, censoring and detection limits, and fixed
train/validation/test boundaries.

Use `/replan` to publish missing context or a revised analysis design as new
tasks. Use `/fail` when authorization, schema, or scientific meaning remains
ambiguous. Before making a result claim, rerun from the exact input hash and
verify the committed output independently.
