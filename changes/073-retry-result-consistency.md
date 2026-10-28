# Consistent retry results

The legacy `aig retry --contract ID` and `aig task retry ID` commands now share
one retry result owner and renderer. JSON success retains the existing top-level
Contract IDs and adds a common `data` envelope containing the actual Candidate,
declaration fact, idempotency key and revision. Errors use the same stable codes
for both spellings. The human success line remains exactly
`Retry contract created: <retry_contract_id>`; the budget and original ID follow
on separate lines. A missing Contract from either spelling is
`{"ok":false,"not_found":true,"error":{"code":"not_found","message":"Contract was not found."},"revision":null}`.
Human success preserves the legacy `Retry contract created:` phrase.

When a retry Candidate was accepted and stored but Redis synchronization is
pending, the nonzero response includes the exact retained retry result. Replaying
the same deterministic retry returns that original declaration and Candidate
IDs, after verifying its durable receipt, declaration and original committed
head. A failure before a decision has no fabricated retry data. Durable
rejections expose only their exact rejection fact IDs.

Retry remains ordinary new signed work with the existing deterministic Contract
and key. It does not reset or replace the original Contract or its claim history.
The existing `retry_local_task` tuple-return function remains available; CLI
adapters use the result-bearing application entry point.

Validation used isolated SQLite/signing fixtures with fixture-backed Redis
fault injection. It did not invoke a real provider or establish native Redis or
installed-artifact behavior.
