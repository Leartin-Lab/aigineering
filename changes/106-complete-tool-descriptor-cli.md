# Complete tool capability descriptors from the CLI

Status: Implemented development increment
Date: 2026-10-23

`capability add-tool` now accepts the full local executable contract fields:
version, optional output schema, and maximum UTF-8 result bytes. Existing calls
retain version `0.1.0`, an empty output schema, and the 1 MiB output limit. The
CLI validates input and output schema documents with the existing tool schema
owner and validates version and byte limit through `ToolSpec` before signing or
publishing a descriptor. An invalid contract is rejected before publication.

`capability show` accepts a logical capability name, the stored Asset name, or
an exact descriptor ID. A unique logical or stored name remains convenient;
multiple matches report each descriptor's name, version, and ID and require
`--id`. Supplying both selectors or neither is an error. This handles multiple
versions and cross-kind name collisions without choosing an arbitrary record.
Add commands report the accepted committed Asset ID so the printed ID can be
used directly with `capability show --id`.

This change does not infer tool success from returned JSON envelopes and does
not change execution-time descriptor resolution. Operators can express a
backend's envelope contract in the published output schema; the runtime still
treats a schema-valid result as an observation according to the existing tool
execution semantics.
