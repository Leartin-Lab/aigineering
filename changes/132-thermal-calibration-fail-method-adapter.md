# Change 132: Thermal calibration fail-method adapter

Status: Implemented. Component and ordinary CLI fixtures cover the adapter;
they do not retroactively qualify earlier failed model trials.

The thermal-calibration verifier now recognizes the current claimed
`/fail` Contract output compiled by `TaskDelegationPlugin`: the sole
`failure_result_<description-hash>` output. It also accepts the example's
explicit compatibility-only `_fail_result_<parent-contract-id>` shape when
the Contract is a well-formed system `plugin:fail` method. Both paths require
the exact method, a nonempty reason, one exact output, and matching nonempty
outer and Contract parent IDs. A malformed labeled Contract or non-system
origin fails with a fixed safe error. A Contract without `plugin:fail` remains
outside this adapter, including the explicit unclaimed `method:fail` builder.

The adapter returns the method's ordinary output Candidate. That output lets
the failure child complete through normal execution; it does not qualify a
parent output, create an attestation, or establish acceptance. The focused
regression constructs the current failure Contract through the real delegation
compiler and exercises the production verifier Worker. Runtime signing and
publication are covered by the passing ordinary CLI integration check, with
independent copied-fact proof; the direct Candidate test remains unsigned.

This change is limited to the example adapter and its regressions. It does not
change generic runtime method semantics, task policy, verifier evidence rules,
or model behavior. The original trial outcomes remain unchanged; this
adapter correction is not evidence of successful provider execution or
independent acceptance.
