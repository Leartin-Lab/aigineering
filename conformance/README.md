# Protocol conformance

Versioned JSON fixtures preserve language-neutral canonical bytes, identities
and signatures. Current Python checks live in `tests/conformance/`; a fixture's
presence does not make its historical effect a supported runtime operation.
The current effect surface is described in [DESIGN](../DESIGN.md).

## Signed JSON boundary

Candidate effect payloads and metadata use this interoperable JSON subset:

- object keys are strings, already NFC and unique after NFC normalization;
- values are objects, arrays, strings, booleans, null, or integers in
  `[-9007199254740991, 9007199254740991]`;
- floating-point values, sets, bytes and language-specific container types are
  forbidden; encode exact decimals as strings.

Canonical JSON sorts object keys by Unicode scalar value, uses compact `,` and
`:` separators, preserves Unicode characters and uses lowercase JSON literals.
Signing bytes are the NFC-normalized canonical JSON encoded as UTF-8. Candidate
IDs are `candidate:v1:` followed by the lowercase SHA-256 of those bytes.

Array order is significant. Builders sort fields whose schema defines set
semantics; the generic encoder does not reorder arrays. Causal parents are
ordered non-empty strings, with duplicates preserved. The v1 decoder rejects
other `protocol_version` values; typed integers obey the same safe range.

```bash
node scripts/check_signed_json_boundary_vectors.mjs
```

## Fixture scope

| Directory or fixture | What it preserves |
| --- | --- |
| `v0.5.0/protocol-vectors.json` | Canonical JSON, Ed25519 keys, Genesis and Contract identities, signed primitive effects and causal allowance identities |
| `v0.5.2/asset-graph-vectors.json` | Normalized content, definition and definition-content assertion identities |
| `v0.5.11/usage-vectors.json` | Usage representation fixtures; presence alone is not provider billing evidence |
| `v0.5.13/signed-json-boundary-vectors.json` | Signing bytes, signatures and invalid JSON/parent cases |
| `v0.5.13/input-binding-vectors.json` | Exact-input Contract identities |
| Other `v0.5.13` fixtures | Historical review, handoff, edit/revision, assignment, dependency, hold/promotion, priority, comment, profile-promotion and attachment shapes |

Retired-writer fixtures remain immutable historical evidence. Python tests
verify their recorded bytes and signatures where covered; they do not rerun
removed projectors or authorize fresh publication. Historical attachment wire
serialization likewise does not restore its removed writer. Current review
methods compile to ordinary primitive effects rather than the historical custom
effects in these fixtures.

The Node consumers independently check the selected fixture's canonical bytes,
signatures and identities:

```bash
node scripts/check_collaboration_vectors.mjs
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/input-binding-vectors.json
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/task-handoff-vectors.json
node scripts/check_task_edit_vectors.mjs
```

Pass another supported `v0.5.13` fixture path to the collaboration consumer to
check its recorded shape. These checks do not establish current task semantics,
claim arbitration, funding conservation, replay admission, installed behavior,
provider quality or a second runtime implementation. Those require their own
behavioral evidence.

Fixed private keys in these fixtures are public test material. Never authorize
them in a real domain.
