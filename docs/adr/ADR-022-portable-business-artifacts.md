# ADR-022: Portable business artifacts and evidence bindings

Status: Accepted for the v0.5.9 local development implementation
Date: 2026-09-07
Related: ADR-015, ADR-017, ADR-018, ADR-020, ADR-021

## Decision

Business adapters compose ordinary Assets, signed Candidates, scoped tools and
independent acceptance. The kernel receives no literature-specific effect,
workflow state, document parser, citation selector or completion rule.

`artifact-v1` is a versioned JSON business envelope with a kind, exact parent
Asset IDs and a payload. Four reference conventions are implemented:

- attachment: bounded original bytes, base64 encoding, byte SHA-256, size,
  media type, source URI, retrieval time and an asserted license;
- document: versioned extraction tool, explicit OCR status and consecutive
  physical page indexes with NFC text, linked to one attachment;
- evidence: an exact nonempty NFC character range within a document page;
- report: Markdown and an exhaustive mapping of footnote anchors to evidence.

Canonical Worker `/exec` continues publishing text through its existing signed
graph compiler. The adapter recognizes the strict envelope schema inside text
or JSON carriers as well as its preferred vendor media type. No name-based
version lookup or separate Worker publication path is introduced.

## Storage choice

Attachments are limited to 8 MiB and carried inside ordinary text Assets. This
is an explicit embedded encoding, not native binary ContentObject support. It
preserves raw byte identity separately from NFC text identity and participates
in existing SQLite commitment, backup and rebuild without a second filesystem
transaction. Base64 and repeated runtime representations cost storage and
context-loading time. Large external blob stores remain future work.

Attachment proposals default to non-promptable. Scoped document tools receive
an explicitly authorized asset snapshot rather than ambient Store access. A
tool result is an observation, never a business output or an attestation.

## Verification and disclosure

The business validator recursively resolves exact IDs, rejects missing or
withheld ancestors, cycles, excessive traversal, malformed encodings, byte
hash mismatch and forged quotes. It enforces no domain-level entailment claim.
Parsing PDF bytes into text is an asserted transformation; exact text selection
does not prove parser fidelity or scientific correctness.

The kernel can accept a structurally valid but semantically invalid artifact
assertion just as it accepts other untrusted outputs. The local artifact ingress
validates business closure before publication; consumers validate again.
Applications requiring accepted findings must retain ordinary independent
Contract acceptance, using an authorized verifier and an exact output Asset ID.

Read/export APIs accept a caller-scoped lookup and reject non-original disclosure
views and tombstones on every hop. The CLI is a local administrative surface.
There is no multi-user authorization service in this release. Citation links
grant neither access nor redistribution rights.

## Acquisition and rendering

OpenAlex/Unpaywall metadata parsing yields candidates with version and license
metadata. A separately invoked public HTTPS adapter uses explicit host scope,
public DNS address validation and pinned connections, bounded bodies and
redirects. It uses no ambient credentials or proxies. Per-socket timeouts do not
claim process-level deadlines. Authenticated archives and institutional access
require future explicitly configured adapters.

PyMuPDF is optional. OCR uses its explicit Tesseract path and does not silently
fall back. No GROBID, Docling, semantic verifier or retraction service is bundled.

Export is a rebuildable presentation: Markdown, escaped evidence HTML with a
highlighted text range, citation map, ancestry metadata and file hashes. Original
attachments are opt-in. PDF links jump to the physical page; coordinate overlays
are not implemented. Runtime ancestry exports contain record identifiers and
selected metadata, not complete signed receipts or portable signature proofs.

## Evidence

Behavior, CLI, scoped ToolRegistry, canonical Harness and reconstruction tests
are recorded in `reports/059-portable-business-artifacts-2026-09-07.md`.
