# Business artifacts (059)

The 059 artifact adapter gives local files a small, portable envelope that can
be carried through the normal Aigineering asset path. An `artifact-v1` envelope
has a kind (`attachment`, `document`, `evidence`, or `report`), an ordered list
of exact parent asset IDs, and a JSON payload. Attachments contain the original
bytes as base64 together with their byte length, SHA-256, media type, source URI,
license, and retrieval time.

The base64 is self-contained business data. It does not add a native blob or a
new kernel fact type. Attachments are limited to 8 MiB. The adapter validates
byte integrity before a document can refer to an attachment. Same names do not
identify versions: use the returned immutable asset ID for every subsequent
operation.

## The publication boundary

`attachment()`, `document()`, `evidence()`, and `report()` return unsigned
proposals. They are pure constructors and do not write the store. Submit each
proposal through an explicitly authorized `CandidatePublisher` using
`asset_proposal_effect`, then take the accepted asset from
`decision.assets[0]`:

```python
from aigineering.business.artifacts import attachment
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.protocol.effect_builders import asset_proposal_effect

proposal = attachment("paper.pdf", pdf_bytes, media_type="application/pdf")
decision = publisher.publish(
    (asset_proposal_effect(proposal),),
    idempotency_key="import-paper-v1",
)
assert decision.accepted
attachment_asset = decision.assets[0]
```

The CLI uses the same Candidate commitment path and stores its local data under
`.aig/store.db` by default. It validates the complete lineage before publication.
Initialize that local Candidate domain before the first CLI publication:

```console
$ aig domain init --domain local --actor human:owner --key-id root-1 --json
```

## Local text and PDF flow

Import a local file first. The import stores a bounded, hashed attachment and
prints its exact asset ID:

```console
$ aig artifact import paper.pdf --name paper-source --media-type application/pdf
{"id":"asset:v1:...","name":"paper-source","kind":"attachment"}
```

Parse that attachment into a versioned page extraction. For plain text, the
stdlib path needs no PDF dependency:

```console
$ aig artifact parse asset:v1:... --name paper-text
{"id":"asset:v1:...","name":"paper-text","kind":"document"}
```

For a PDF, the same command uses optional PyMuPDF. Add `--ocr` only when the
local PyMuPDF OCR support and Tesseract installation are available:

```console
$ aig artifact parse asset:v1:... --name paper-pdf-text --ocr
```

OCR is explicit and errors are surfaced; it never silently falls back to
embedded text. Extracted pages use NFC-normalized text and stable one-based
physical page numbers. Evidence uses zero-based NFC character offsets and an
exclusive end bound:

```console
$ aig artifact cite asset:v1:... --name abstract-claim --page 1 --start 120 --end 188
{"id":"asset:v1:...","name":"abstract-claim","kind":"evidence"}
```

Create Markdown with footnote markers and a JSON object binding every marker to
an exact evidence asset ID:

```console
$ cat report.md
The intervention improved recall.[^claim]
$ cat bindings.json
{"claim":"asset:v1:..."}
$ aig artifact report report.md --bindings bindings.json --name recall-report
```

The binding must match the Markdown markers exactly. Footnote definitions in
the input are rejected because the exporter owns their rendering.

## Validation and export

`aig artifact lineage REPORT_ID` resolves every parent by exact ID and verifies
the complete bounded graph. A missing or withheld parent fails closed. Sealed
or tombstoned assets cannot enter envelopes, derived documents, reports, or
exports.

```console
$ aig artifact lineage asset:v1:...
$ aig artifact export asset:v1:... --output ./paper-export
$ aig artifact export asset:v1:... --output ./paper-export-with-source --include-sources
```

Exports include Markdown, generated evidence pages, citation metadata, a
lineage graph, and a manifest. Original attachments are omitted by default;
`--include-sources` is an explicit redistribution choice and does not grant a
license. Rendered source text is escaped for HTML.

The lineage result proves exact quote selection and attachment byte integrity.
It does not prove parser fidelity, and it does not provide semantic support or
entailment. Those remain outside this adapter's claim.

## Full-text locations

`aig artifact locations` accepts already downloaded OpenAlex or Unpaywall
metadata and returns safe candidate URLs. It supports both providers, filters
obvious unsafe URLs and credentials, and does not fetch content or grant access:

```console
$ aig artifact locations openalex-record.json --provider openalex
$ aig artifact locations unpaywall-record.json --provider unpaywall
```

Fetching, access decisions, and subsequent import are separate operations. The
059 implementation has no GROBID or Docling integration, and it does not claim
production isolation or automatic LLM research. It provides bounded local
packaging, exact IDs, deterministic quotations, and auditable export through
the existing signed Candidate runtime.

## Public acquisition

When a location is a public HTTPS resource, `aig artifact fetch` can acquire it
with an explicit host allow-list and then commit the bytes as an ordinary
attachment. Initialize the local domain first, then provide `--allow-host` for
the requested host; repeat the option for hosts that are explicitly permitted
as redirect targets:

```console
$ aig domain init --domain local --actor human:owner --key-id root-1 --json
$ aig artifact fetch https://publisher.example/paper.pdf \
    --allow-host publisher.example --name paper-source
```

The acquisition adapter uses HTTPS on port 443, bounded size and timeouts,
pinned public DNS answers, and at most three in-scope redirects. It sends no
ambient cookies, proxy credentials, API keys, paid-archive credentials, or
other ambient credentials. `--allow-host` is an exact host scope, not a grant
of access to a private or paid archive. A failed request, unsafe redirect,
non-200 response, or truncated transfer produces an error and is not an
accepted attachment. Only after acquisition succeeds does the CLI construct an
attachment and pass it through the ordinary signed Candidate acceptance path.

## Canonical Workers and scoped tools

Canonical `/exec` outputs can carry the same JSON envelope in their ordinary
text content. The adapter validates the schema rather than requiring a new
Worker compiler or kernel media-type branch. Constructors do not grant output
authority: the name must still be declared by the claimed Contract.

`aigineering.business.tools.build_registry(assets=(...))` registers
`document_extract`, `artifact_verify`, and `fulltext_locations`. Pass only the
explicitly authorized snapshot for that tool scope; the handlers do not open an
ambient database or resolve file paths. Tool results remain local observations.
Use ordinary independent acceptance for conclusions that need qualification.

CLI lineage and export additionally include the producing committed-record
identifiers and their available causal ancestors, with actor/task metadata.
They omit raw receipts; the exported graph is not a standalone signature proof.
Any unresolved runtime parents are listed separately. The evidence HTML
highlights extracted text and can link to the PDF page; it does not overlay a
bounding box in the original PDF.
