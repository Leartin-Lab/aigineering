# Atomic HTTP slice publication and relay reporting

Status: Complete

## HTTP slice

Fresh requests to `POST /assets/{name}/slice` sign an ordered pair of
`asset.propose` and `asset.relate` effects. The relation supplies the exact
source Asset ID and the range. It uses `replacement_effect_index=0` to bind the
relation to the projected Asset. The optional top-level `range` must agree with
the signed range when supplied.

The endpoint verifies the fresh derivation against the exact source under the
Candidate writer transaction. For a graph-backed source, it reads the exact
Definition and Content values by ID. The Asset and relation share one receipt,
committed head, and transaction. The response retains the Asset representation.
Signed requests replay their original committed members after current-key
authentication and closure verification, even when the source name has gained
new versions. Previously committed single-effect slices can replay their
original publication when the Store has a complete, verifiable closure.

## Relay reporting

HTTP and CLI pending responses correlate a relay failure with the observed
SQLite revision and the available committed identifiers. These can include a
Candidate, receipt, head, affected Assets and Contracts, or operational claim.
An authentication rejection reports its durable audit record ID. Preflight
unavailability remains distinguishable from post-commit pending. Signed
material and Worker packages stay outside pending responses.

Manual output and method attestation commands relay their committed decisions
before reporting success. Retrying the same signed action can synchronize the
existing facts and return the original result.
