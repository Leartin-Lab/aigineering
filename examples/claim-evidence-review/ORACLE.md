# Manual oracle

Use this file only to review a completed report; it is intentionally separate
from the committed task input.

- `claim-1` may be marked `supported` for the reported group comparison, but
  its revised wording should stay descriptive and should not imply that sleep
  caused the score difference.
- `claim-2` is `overstated`: the cross-sectional, self-reported measurements
  and lack of random assignment or follow-up do not establish improvement or
  causation. A calibrated revision can describe an association.
- `claim-3` is `overstated` or `unsupported` depending on the report's exact
  interpretation, because two-campus sampling and one examination outcome do
  not justify a broad population claim.

Every verdict must cite only the excerpt IDs that appear in the input. A sound
report mentions cross-sectional timing, self-reported sleep, two-campus
sampling, and residual confounding or related limits. It should preserve the
observed comparison without manufacturing effect sizes or omitted study facts.

The claim IDs must match the input exactly, each appearing once. Every
`original_text` must equal the original `claims[].text` verbatim; a correct-looking
verdict for an invented replacement claim fails this oracle. Statements that the
sampling location is unspecified contradict the explicit two-campus source.

Any excerpt used by a rationale must appear in that claim's evidence_excerpt_ids.
In particular, a rationale referring to the limitations excerpt must list
`excerpt-limitations`; correct verdicts do not excuse incomplete attribution.
