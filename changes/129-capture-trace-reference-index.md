# Change 129: Index typed Candidate heads during Redis capture

## Result

Redis event capture builds a capture-local, multi-value Candidate-head index
from the effective by-ID mapping. Commitment traces select exact-string matches
in original order and resolve exact committed members through the existing
recursive reader. Session export, CLI/Fleet cycles and audit retain their
existing Mapping path.

Non-exact-string IDs retain the original ordered scan and lazy errors. Empty
mapping semantics, duplicate effective-ID handling, multiple heads, recursive
`visited`, event fields and error propagation remain unchanged. No persistent
cache, Store interface, Redis protocol shortcut, fact or authority is added.

## Limits

Equivalence checks must compare complete event fields and digests with the
reference proof, including malformed histories. The capture still reads the
complete historical prefix; index selection does not establish active-work
locality or total warm-sync complexity.

An intermittent Fleet failure observed during earlier integration remains
unattributed. A later passing rerun does not resolve that observation or prove
general stability. Deterministic index checks do not replace native Redis and
installed-workflow checks.
