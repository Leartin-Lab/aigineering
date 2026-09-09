# 017: Portable business artifacts

Version: v0.5.9
Date: 2026-09-07

The local reference implementation adds bounded self-contained attachments,
versioned page extraction, exact evidence selection, Markdown citation bindings,
recursive ancestry and deterministic offline exports. All durable publications
reuse signed Candidates; no kernel schema, completion rule or authority changes.

The `aig artifact` CLI operates on exact Asset IDs. PDF/OCR support is optional;
UTF-8 text exercises the same conventions without a PDF dependency. Explicit
ToolRegistry composition binds extraction and verification to an authorized
asset snapshot. Canonical Harness output can carry the same envelope as text.

OpenAlex and Unpaywall location adapters preserve source/version metadata. Public
HTTPS acquisition is bounded and host-scoped, with a retained successful-fetch
receipt. Authenticated full-text services are outside this implementation.

Byte integrity and exact quotation are verified mechanically. Semantic support,
parser fidelity, independent scientific corroboration and retraction checks are
not inferred from a valid lineage graph. See ADR-022 for storage tradeoffs,
disclosure requirements and export limits.
