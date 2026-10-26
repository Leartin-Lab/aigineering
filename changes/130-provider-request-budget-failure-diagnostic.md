# Show the fixed request-budget failure category in diagnostics

Task attempt diagnostics now recognize the fixed
`provider_request_budget_exhausted` Worker failure code. The existing exact
claim proof remains required before the category appears in task runs,
diagnostics, or shared run failure details. Unknown categories and arbitrary
Worker text remain suppressed.

This changes only read-time classification. Request admission, transport,
failure facts, claim release, recovery, usage accounting, and task state are
unchanged. The displayed category is a fixed, reported failure classification. By itself
it does not prove physical request admission, provider receipt, or billing;
those conclusions require separate transport and meter evidence.
