# AI4S scientific data profile example

The public examples now include an installable `scientific-data-profile`
skill. It maps table manifest, profiling, analysis design, execution, and
verification onto ordinary Aigineering tasks without adding data-science
semantics or dependencies to the runtime kernel.

Its standard-library CSV/TSV adapter enforces an explicit filesystem root,
rejects traversal and linked or special files, bounds input size and scanned
shape, validates rectangular rows, hides field names by default, and never
emits cell values. The adapter binds its aggregate profile to an exact input
SHA-256 and emits one `/exec` action for the canonical harness boundary.
