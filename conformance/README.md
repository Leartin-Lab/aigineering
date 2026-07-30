# Protocol conformance

The versioned JSON files in this directory are language-neutral inputs and
expected outputs for independent Aigineering protocol implementations. The
Python reference consumes the same files in `tests/conformance/`.

## Signed JSON domain

Candidate effect payloads and metadata use this interoperable JSON subset:

- object keys are strings;
- values are objects, arrays, strings, booleans, null, or integers in
  `[-9007199254740991, 9007199254740991]`;
- floating-point values are forbidden; encode exact decimals as strings;
- sets, bytes, tuples as a distinct wire type, NaN, and infinities are
  forbidden.

Canonical JSON sorts object keys by Unicode scalar value, emits compact `,` and
`:` separators, preserves Unicode characters, and uses lowercase JSON literals.
Candidate signing bytes are the NFC normalization of that complete canonical
JSON string encoded as UTF-8. Candidate IDs are `candidate:v1:` plus the
lowercase SHA-256 hex digest of the same NFC-normalized string.

Array order is significant. Builders must sort protocol fields whose schema
defines set semantics before constructing the Candidate; a generic canonical
JSON encoder must not reorder arrays.

The v1 decoder rejects any `protocol_version` other than `1`. Typed integers
inside the signed envelope, including `claim_epoch`, obey the same safe range.

## v0.5.0 vector

`v0.5.0/protocol-vectors.json` covers canonical JSON, a non-secret fixed
Ed25519 test key, Genesis identity, Contract v3 identity, signed
`contract.declare`, signed `asset.attest`, and causal-allowance identities. The
private key is test material only and must never be authorized in a real domain.

## v0.5.2 Asset graph vector

`v0.5.2/asset-graph-vectors.json` covers normalized content identity, signed
definition bytes and identity, and signed definition-content assertion bytes
and identity. Decimal similarity values are strings; no language-specific
floating-point serialization participates in signatures.
