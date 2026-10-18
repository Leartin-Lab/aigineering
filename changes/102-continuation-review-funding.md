# Change 102: Preserve review funding across tool continuations

Eligible business Contracts with independent acceptance now retain up to one
unit of their existing causal allowance per declared output when a single-tool
continuation is published, subject to keeping at least one unit for the child.
The continuation receives the remaining allowance. Non-eligible and
non-independent parents keep their existing continuation allocation.
Continuation-origin parents are not charged a second review reserve.

Completion fixes each continuation intent from the exact accepted declaration
head of its source Contract, including the source-time allowance batch. On
restart it recognizes an already accepted child only from its exact original
Candidate intent, receipt, canonical Contract, complete committed batch and
configured publisher actor/domain; it does not re-sign that child. When no child
exists, the fixed intent is sent through the current signed publisher. A later
authorized spend can make the fixed budget unaffordable; the writer then rejects
it visibly rather than shrinking the child. An existing failed parent terminal
is not cleared or repaired by completion replay.

Parallel tool publication applies the same review demand through one plugin-owned
allocation helper. It combines the review demand with the existing bounded
first-recovery demand in the parent's ordinary balance, while preserving at
least one unit for the join when the tool tasks and join are affordable. When
both demands do not fit, the remaining balance stays shared and carries no
priority or earmark. Calls plus the join must still fit the original allowance;
the existing builder refusal remains visible when that minimum is unavailable,
with no child declaration or allowance reservation.

Review requests still use the existing eligible-source predicate, signed
publication, exact-output ancestry, independent policy, writer checks, and
canonical attestation path. The default reserve funds only the existing
one-unit review request per output; it does not promise verifier availability,
a larger review budget, or acceptance. No allowance return, Store or kernel
owner, protocol change, scheduler, or continuation-review exception was added.
Stop older persistent Fleet and engine processes before upgrading; compatible
stored facts do not guarantee coordination across completion implementations.

Verification: [`tests/test_continuation_review_funding.py`](../tests/test_continuation_review_funding.py)
uses signed local ToolWorker fixtures to publish a single-tool or healthy parallel
continuation, submit exact descendant reports, request root review, run a distinct
reviewer, accept through the canonical review decision, reopen SQLite, and export
the exact qualified root Asset through `task result`. The tests cover multiple
outputs, nested continuations, restart between accepted publication and completion
marker, exact legacy-child recognition after publisher key rotation, current
publisher authorization, incomplete history, bounded multi-engine replay, current
writer allowance refusal, one-time reserve, low allowance, and exact export. [`tests/test_parallel_recovery_funding.py`](../tests/test_parallel_recovery_funding.py)
covers minimum/shared parallel recovery balance, the original call-plus-join
budget refusal, non-independent recovery, review replay, and accepted parallel
history. These are fixture-backed tests, not actual model/provider execution.
