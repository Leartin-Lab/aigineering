# v0.5.9 portable business artifact evidence

Date: 2026-09-07
Baseline: `d5d2470` (v0.5.8).
Scope: local development implementation on `dev`; no remote publication claim.
Environment: macOS arm64, project Python 3.11.15, SQLite 3.53.4.

## Implemented closure

`artifact-v1` composes ordinary signed Assets into an attachment, document,
evidence and report chain. It introduces no kernel effects, database schema or
task-completion semantics. The installed `aig artifact` commands import/fetch,
parse, quote, bind citations, inspect recursive ancestry and export a new private
directory. ToolRegistry handlers are bound to explicit asset snapshots.

The default attachment limit is 8 MiB. Original bytes are embedded as base64
with separate SHA-256 and size checks. Exports omit original files unless
explicitly requested. Exact quotation is checked against immutable NFC page
text. Parser fidelity and semantic entailment are explicitly not evaluated.

## Behavior evidence

The new tests cover:

- real signed Candidate publication and SQLite reconstruction, not direct writes;
- byte preservation, corrupt encoding/hash rejection, missing/withheld ancestors;
- exact page/character ranges, forged quotation rejection and same-name versions;
- exhaustive Markdown bindings, unsafe/case-colliding anchors and duplicate JSON;
- HTML escaping, source opt-in, file hashes and refusal to overwrite exports;
- PyMuPDF parsing of a generated two-page PDF, malformed PDF handling and explicit
  mocked OCR, strict UTF-8 and page-count limits;
- Unpaywall/OpenAlex fixture normalization without inventing access rights;
- public HTTPS size/status/redirect limits, private/mixed DNS rejection, pinned
  addresses and original-host TLS SNI, with mocked transports;
- explicit ToolRegistry scope and schema checks;
- a canonical Harness claim and `/exec` report, using an ordinary text carrier;
- AST ownership guards excluding business semantics from core/protocol and
  excluding direct runtime writes from business adapters.

Full reconstruction tests use the backup-first diagnostic and verify unchanged
attachment bytes, ancestry and deterministic export hashes from rebuilt SQLite.

## Live public-file check

The public HTTPS adapter retrieved `https://arxiv.org/pdf/1706.03762` with HTTP
200, 2,215,244 bytes and SHA-256
`bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697`.
PyMuPDF 1.27.2.3 extracted 15 pages. A source-bound character quotation and report
were published through four signed Candidates and exported with the original
PDF. The diagnostic rebuilt the database with matching semantic digest
`d4754372e92c3538b92af321a593dddfa60daab87af58a2bd15e0e49823ff39e`.
Reopening the rebuilt database preserved the original PDF bytes and ancestry.

Non-content evidence is retained in `reports/data/059-artifact-live.json`.
The original PDF, exported passage, private fixture database and receipts remain
under `/private/tmp/aigineering-059-live-yxyhxfuz`, outside the repository.
This checks acquisition and provenance, not the paper's scientific conclusions.

## Distribution and verification

Ruff lint/format and `git diff --check` passed. An isolated `python -m build`
produced wheel and sdist; Twine accepted both. The installed-wheel smoke runs
outside the source tree in a fresh environment and exercises signed ingress,
an explicit mock task, artifact import/parse/cite/report/export in independent
CLI processes, source reopen and backup-first reconstruction. Its retained
diagnostic is under `/private/tmp/aigineering-059-installed-evidence`. The final
distribution is assembled with `build --no-isolation` after the isolated-build
check so the sdist includes the closing evidence, and receives its own installed
smoke check under `/private/tmp/aigineering-059-final-installed-evidence`.

The initial system-Python suite passed 1,286 tests with three optional dependency
skips. Final API/Redis/PDF validation uses the project virtual environment with
PyMuPDF 1.28.2 and a dedicated temporary Redis 8.4.0 container:
`AIG_REDIS_TEST_URL=redis://127.0.0.1:32769/15 .venv/bin/python -m pytest -q`
passed **1,289 tests, no skips**, in 57.65 seconds. The six warnings are dependency
deprecations (Starlette/httpx and PyMuPDF SWIG). The container was removed after
validation. CI installs the
documents extra to exercise real PDF parsing; remote CI execution is not claimed.

## Explicit limits

There is no fresh live-LLM research qualification, OCR-engine execution,
authenticated OpenAlex archive or institutional-access check in this release.
No GROBID/Docling integration, semantic cross-verification, retraction adapter,
PDF coordinate overlay or automatic literature workflow is claimed. Tool socket
timeouts are not process-level cancellation. Embedded bytes are unsuitable for
large libraries; external blob lifecycle and crash-atomic references remain
future work. Runtime ancestry exports are metadata, not standalone signed proofs.

This is the first reusable asset/citation closure, not a claim that all research
quality or stable-publication gates have been completed. Remote `main` and `dev`
are not advanced by this local development work.
