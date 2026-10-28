# Synthetic temperature calibration drift fixture

This package defines deterministic, synthetic calibration cases for checking
unit normalization, time ordering, per-sample bias, end-to-end drift, tolerance
decisions, and evidence completeness. It is a software fixture, not a real
sensor assessment or an industry calibration procedure. The package contains
no runtime scheduler, persistence adapter, provider, or claim of confidential
evaluation data.

## Input contract

The case is one JSON object with exactly these keys:

```json
{
  "schema": "thermal-calibration-case-v1",
  "case_id": "case identifier",
  "sensor_id": "sensor identifier",
  "samples": [
    {
      "sample_id": "unique row identifier",
      "time_utc": "2026-01-01T00:00:00Z",
      "reference_C": "20.000000",
      "reading": "68.000000",
      "unit": "F"
    }
  ]
}
```

There must be 2–256 samples. `sample_id` values are nonempty and unique.
Timestamps use exactly `YYYY-MM-DDTHH:MM:SSZ`, are valid UTC instants at whole
second precision, and increase strictly in input order. `reference_C` and
`reading` are strings matching `-?(0|[1-9][0-9]{0,11})(\.[0-9]{1,6})?`.
Scientific notation, JSON numbers, booleans, NaN, infinity, leading plus signs,
and excess precision are rejected. `unit` is exactly `C`, `F`, or `K` and
applies to `reading`; `reference_C` is always Celsius. Unknown or missing keys,
values, rows, or schema versions are rejected. The literal input strings remain
part of source identity even where two strings represent the same number.

The standard is one JSON object with exactly these keys:

```json
{
  "schema": "thermal-calibration-standard-v1",
  "standard_id": "synthetic-temperature-calibration-v1",
  "max_abs_error_C": "0.200000"
}
```

The tolerance is a nonnegative decimal string under the same numeric grammar.
No other acceptance threshold or inferred real-world meaning is implied.

## Tool interface and normalized evidence

`build_registry()` returns two pure tools:

- `normalize_calibration` takes `{"case": CASE, "standard": STANDARD}`.
- `analyze_calibration` takes `{"normalized": NORMALIZED}`.

The normalization result has exactly `schema`, `case`, `standard`,
`case_sha256`, `standard_sha256`, and `samples`. It preserves the complete
original case and standard; hashes are SHA-256 of their canonical JSON bytes
(UTF-8, sorted keys, compact separators, no ASCII escaping). Each normalized
sample has exactly `sample_id`, `time_utc`, `reference_C`, `reading_C`,
`bias_C`, and `source_evidence`. `source_evidence` has exactly `case_sha256`,
`sample_id`, and `field_roles`; roles are the ordered list
`["time_utc", "reference_C", "reading", "unit"]`. The analysis tool
validates the embedded originals, recomputes their hashes and every normalized
row, and refuses any missing, extra, or modified member before calculating a
report.

The report has exactly `schema`, `case_id`, `sensor_id`, `case_sha256`,
`standard_sha256`, `standard_id`, `sample_count`, `samples`,
`aggregate_evidence`, `drift_C_per_hour`, `max_abs_error_C`, and
`within_tolerance`. Its `samples` array contains every normalized row in
original order, with the same source-evidence member. `aggregate_evidence` has
exactly `case_sha256`, `standard_sha256`, `sample_ids`, and `standard_fields`;
the IDs include every source row in order and `standard_fields` is exactly
`["max_abs_error_C"]`. `within_tolerance` is computed from the unrounded
maximum absolute bias using `<=` the frozen standard threshold.

Tool arithmetic parses each exact decimal input into integer micro-units and
uses integer rational numerators for C/F/K conversion, bias, drift, maximum
error, and tolerance comparison. No repeating conversion is rounded between
operations; values are rounded only when rendered for six-place display.
For readings, Celsius is unchanged, Fahrenheit converts as `(F - 32) * 5 / 9`,
and Kelvin as `K - 273.15`. Each bias is `reading_C - reference_C`. Drift is
`(last_bias - first_bias) * 3600 / elapsed_seconds`; positive drift means the
sensor reading rises relative to its reference over time. Maximum absolute
error is the largest absolute per-sample bias. Derived decimal strings are
rounded once for display to six fractional places with `ROUND_HALF_EVEN`;
rounded negative zero is written as `0.000000`. The tolerance decision uses
unrounded values and is unaffected by display rounding.

## Independent oracle

`evaluate_report(case, standard, report)` independently parses the frozen
source and recomputes expected values with exact rational arithmetic. It does
not call either tool or trust normalized tool output. It checks strict report
shape, source and standard hashes, every sample row, the aggregate values and
the unrounded tolerance result. It returns exactly:

```json
{
  "passed": true,
  "errors": [],
  "checked_case_sha256": "...",
  "checked_standard_sha256": "...",
  "checked_sample_count": 3
}
```

Malformed source data raises `ValueError`; a malformed or incorrect report
returns `passed: false` with nonempty error codes. Oracle `passed: true` means
the report correctly and completely describes the supplied inputs. A correct
report with `within_tolerance: false` is still a passed evaluation; it records a
sensor outside this synthetic standard, not a failed task or model. A false
claim of `within_tolerance: true` is rejected. The oracle is an external
fixture evaluator. It does not sign, publish, attest, or create runtime facts.

## Hand-check vector

For three samples at 00:00, 00:20, and 00:50 UTC, use references `20`, `20`,
`20` °C and readings `68` °F, `293.15` K, and `19.8` °C. Normalized readings
are `20`, `20`, and `19.8` °C, so the biases are `0`, `0`, and `-0.2` °C.
Elapsed time is 3,000 seconds; drift is `(-0.2 - 0) * 3600 / 3000 = -0.24`
°C/hour, maximum absolute error is `0.2` °C, and a `0.2` °C threshold passes
at equality. The report displays `-0.240000` drift and `0.200000` maximum error.

## Fixed datasets and limits

`fixtures/train/` and `fixtures/eval/` contain versioned synthetic JSON cases;
their manifests bind IDs to canonical content hashes and the standard version.
The eval partition is only relatively held out from test-driven tool tuning.
Because this repository is public, it is not a secret benchmark. The oracle's
100% gate applies only to the enumerated local vectors. The oracle vectors alone establish neither CLI delivery reliability nor a
quality/cost advantage over a direct Harness.

## Bounded ordinary Runtime profile

The optional source-checkout `g0c-runtime-v2` profile keeps the ordinary Planner,
Researcher, tool, continuation, claim, commitment, and independent-verifier
owners. It changes the Researcher profile to thinking disabled; this is a
bounded hypothesis, not evidence that thinking caused an earlier failure.
The complete report and both complete tool observations remain required by
the Skill and deterministic verifier.

Planner and Researcher share a process-local meter: 8 attempts, an 8,192
output-token cap per attempt, a 65,536 aggregate output reservation ceiling,
180-second cooperative deadline, 60-second request timeout, and zero retries.
The root's 32-unit causal task allowance is separate from those request limits.
The Planner still chooses ordinary tasks dynamically. Configured request limits
are not a task graph or a guarantee of available recovery funds.

The ordinary CLI requires native Redis for discovery, claims, and event relay.
The existing fake-provider CLI tests preserve that Runtime path and therefore
need an explicitly assigned isolated Redis lease. Their provider replies are
fixtures, not live provider evidence. No fixture or scheduled recovery alone
proves report completion or independent acceptance.
