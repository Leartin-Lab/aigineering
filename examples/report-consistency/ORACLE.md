# Human review oracle

This file is for a human reviewer only. Do not load it as a Skill, Asset, task
input, or Worker context.

For `source-data.json`, the corrected monthly sales values are `[120, 150, 200]`
and the corrected total is `470`. The draft's `2026-03` value `180` and total
`450` are wrong. A passing audit identifies both mismatches, preserves the
source report ID and currency, and cites the committed source Asset ID.
