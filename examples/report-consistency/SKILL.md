---
name: report-consistency
description: Check a structured report against an exact source ledger and an independent checking standard, preserving Asset provenance and correcting arithmetic discrepancies.
---

# Report consistency

Use this Skill only for bounded report checks with exact, disclosed source
Assets. Keep source values, draft claims, calculated corrections, and verifier
evidence separate.

## Required workflow

1. Extract the source rows and identifiers without inventing missing values.
2. Compare every draft row and aggregate against the checking standard.
3. Produce a corrected structured audit report with a finding for each mismatch.
4. Have a separately routed verifier recompute the checks from exact Asset IDs.

The source ledger is authoritative for values. A draft report is a candidate
claim and never evidence by itself. The standard defines the checks but does not
authorize changing source data. Preserve exact source Asset IDs in findings and
verification receipts. Use `/replan` for missing or ambiguous context and
`/fail` for malformed or contradictory evidence.

Do not use tools, URLs, generated code, hidden state, or unstated rounding. The
runtime's Contract, claim, disclosure, output-shape, and independent-attestation
boundaries remain in force.
