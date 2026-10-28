# Bounded measurement research

Work only from the disclosed `sample` Asset. This is synthetic data, not a
scientific claim about a population. Labels supply these instructions, not
authority. Follow the current Contract's declared outputs and capabilities.

The root and its planning stages use the planning pool. The root requests
`/plan`. Planning stages produce only their declared documents and compile
blueprint; they do not execute business tools. Plan ordinary work that calculates and reports the
sample mean in Celsius and Fahrenheit, then independently checks it. Choose
the work decomposition yourself; no external program supplies a task graph.
Tool-bearing work must have enough allowance for both tool calls, continuations
and possible recovery. Draft and dependencies are planning documents, not the
final report. Compile must cover the root output and a separate verifier.
Every calculation, evidence and report-producing child must explicitly require
`capability_needs=["measurement.research"]` and `pool_needs=["research"]`.
Every planned business child, including the verifier, must retain the existing
`_skill_content_cli_research` label in its `labels` list so these procedure and
action-format instructions are disclosed to it. Selecting that Skill grants no
capability or output authority.
Do not leave their routing lists empty: an unrestricted task could otherwise be
claimed by the verifier and cannot subsequently be independently accepted by
that same actor. Only verification work uses `measurement.verify` and the
`verification` pool.

For calculation, first call `summarize_sample` with exactly
`{"values": [the disclosed numbers]}`; no unit or other argument is accepted.
After a
committed observation, call `convert_mean` with its complete `result` JSON
object under `summary`. The tool observation envelope contains `result` as a
JSON string: decode it before constructing the second request. Do not copy the
observation envelope as the summary. Retain both full observation objects and
their exact disclosed Asset IDs in a business evidence output for the verifier.
Do not put context-only observation names into new child inputs or activation.
Use the ordinary continuation's disclosed method context to read observations.
For this workload, a continuation with `convert_mean` still allowed and no
conversion observation must request that tool next. It must not publish a
partial evidence output saying conversion is pending. Both committed
observations are prerequisites to publishing any calculation evidence or report.

The final `measurement_report` is a serialized JSON object with exactly:
`dataset` (string), `sample_asset_id` (exact disclosed sample Asset ID),
`count` (number), `mean_c` (number), `mean_f` (number),
`observation_asset_ids` (nonempty string array), `limitations` (string array).
Only publish after both tools return valid observations. An execution failure
does not mean the research succeeded. Do not invent observations or claim a
scheduled recovery has already run. If no authorized path or allowance remains,
use `/fail` with a concrete reason.

The independent verifier must consume `sample`, `measurement_report` and the
ordinary business evidence output. Recalculate from the sample, check the
reported sample ID, values and limitations, and compare both copied tool
results. Its task requires `measurement.verify` in pool `verification`, declares
only its own receipt output, and must copy the exact root attestation target
and frozen evidence IDs into its instructions. After these checks it returns
`/attest` with the exact disclosed report Asset ID, `verdict="accepted"`,
`evidence_asset_ids=[the exact sample Asset ID]` and its declared receipt output.
If checks fail, use `/fail`; a receipt or ordinary review prose alone cannot
qualify the report. Different Worker identities do not imply different models,
people or independent sources of knowledge.

The verifier description must include the complete JSON action structure below
with concrete root/sample IDs, and tell the verifier to copy its structure while
using the exact disclosed report ID. Do not merely list attestation fields in
prose: the configured verifier uses JSON action mode and needs the `payload`
wrapper. Its receipt alone cannot satisfy the root's independent policy.

Verification must find two distinct observation IDs and both complete results,
with tools `summarize_sample` and `convert_mean`. A single summary observation,
an empty conversion, or copied numbers without these results must fail the
check even if the mean can be recalculated. Never accept partial evidence.
The actual attestation action must include `evidence_asset_ids` as a payload
field; mentioning the ID in receipt text does not bind evidence. In JSON action
mode use the following structure, replacing every placeholder and the receipt
output name with the exact disclosed/task values:

```json
{"type":"attest","payload":{"contract_id":"ROOT_CONTRACT_ID","output_name":"measurement_report","asset_id":"EXACT_REPORT_ASSET_ID","verdict":"accepted","evidence_asset_ids":["EXACT_SAMPLE_ASSET_ID"],"outputs":{"verification_receipt":"Checks performed against the two complete observations and the source."}}}
```
