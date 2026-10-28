# Select the uniquely matching disclosed tool descriptor

Status: Implemented development increment
Date: 2026-10-23

ToolWorker now examines every disclosed descriptor for the requested tool and
selects the unique valid descriptor whose signed executable fields match the
registered ToolSpec. Descriptor ordering no longer decides execution. Repeated
entries with the same Asset ID count as one descriptor; two distinct IDs that
both match fail with `tool_descriptor_ambiguous` before the handler runs.

No valid descriptor with the requested disclosed name retains
`tool_descriptor_invalid`. At least one valid descriptor with no match retains
`tool_descriptor_mismatch`. The Worker checks only the disclosed Assets and
current registered ToolSpec; it does not query or refresh Store inputs, select
by version preference, or add undisclosed descriptors. Errors use the existing
safe WorkerExecutionError boundary and contain no descriptor payload.

Tests cover order-independent matching, old-version-only mismatch, distinct
matching descriptor ambiguity, duplicate Asset IDs, missing and invalid
descriptors, handler non-execution on rejection, and the existing negative
business JSON observation behavior.
