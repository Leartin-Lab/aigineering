# Protocol conformance

The versioned JSON files in this directory are language-neutral inputs and
expected outputs for independent Aigineering protocol implementations. The
Python reference consumes the same files in `tests/conformance/`.

## Signed JSON domain

Candidate effect payloads and metadata use this interoperable JSON subset:

- object keys are strings;
- object keys are already NFC and unique after NFC normalization;
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

Candidate causal parents are arrays of non-empty strings. Parent order and
duplicates are preserved; references are not required to resolve or use a
particular ID prefix. The shared v0.5.13 boundary vectors include a fixed-key
Candidate with the valid NFC-key object and ordered parents, expected signing
bytes, ID and signature, plus invalid key and parent cases. Python and Node
consume and verify the same file:

```bash
node scripts/check_signed_json_boundary_vectors.mjs
```

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

## v0.5.13 task-comment vector

`v0.5.13/task-comment-vectors.json` covers a signed `task.comment` Candidate and
its projected `task.commented` record, including Unicode scalar key ordering and
NFC signing bytes. The fixed private key is public test material and must never
be authorized in a real domain. Python consumes it in `tests/conformance/`.
An independent consumer verifies bytes, Ed25519 signature and both identities:

```bash
node scripts/check_collaboration_vectors.mjs
```

This checks protocol interoperability for that vector, not a second runtime
implementation or every collaboration operation.

`v0.5.13/task-hold-vectors.json` adds four signed transitions: two independent
placements and their exact releases. Python consumes the same file; the Node
consumer verifies its canonical identities and signatures:

```bash
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/task-hold-vectors.json
```

The same public test-key restriction applies. Transactional claim arbitration is
verified separately by behavioral and concurrency tests.

`v0.5.13/task-handoff-vectors.json` adds explicit-budget and nullable-budget
held-successor projections, including successor wire values, cancellation,
declaration, hold and attributed link identities. Each case starts from the
same isolated pure context. Python consumes the file; Node independently checks
canonical signing bytes, Ed25519 signatures and Candidate/runtime-record IDs:

```bash
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/task-handoff-vectors.json
```

These vectors do not establish Store claim fencing or transaction concurrency;
those guarantees have separate behavioral tests. The same public test-key
restriction applies.

`v0.5.13/task-review-vectors.json` adds exact-context review requests and both
accept/change decisions. Frozen input facts and expected ordinary reviewer
Contracts are included for Python pure projection. Node independently checks
signing bytes, Ed25519 signatures and Candidate/runtime-record identities:

```bash
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/task-review-vectors.json
```

These checks do not establish provider quality, independent people, acceptance
policy compliance beyond the fixture, or transactional concurrency. The fixed
private key is public test material and must never be used in a real domain.

`v0.5.13/input-binding-vectors.json` contains two task:v6 identity cases, including
an empty map and a retained signer policy. There are no Candidate effects in this
file: Node reports signature checks as not applicable and independently recomputes
full Contract identities. `v0.5.13/task-revision-vectors.json` covers explicit and
nullable budget plus exact replay after source cancellation, with signed revision
Candidates, projected facts and v6 successors:

```bash
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/input-binding-vectors.json
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/task-revision-vectors.json
```

Python consumes both files in `tests/conformance/`. These fixtures do not establish
transactional arbitration, migration compatibility, provider quality or semantic
acceptance; those require their own evidence. The same public test-key restriction
applies.

`v0.5.13/task-dependency-vectors.json` covers one signed prerequisite
placement and its exact release. The Node consumer checks the single-effect wire
shape, signed bytes and identities, receipt/link causal references, and that a
release preserves the exact endpoint pair. Python consumes the same facts
through the pure dependency projector. These vectors establish wire
interoperability, not SQLite claim arbitration or concurrent link/claim
behavior; those are covered by runtime tests.

`v0.5.13/expanded-handoff-vectors.json` covers a three-level descendant tree,
explicit and nullable successor budgets, and exact replay after cancellation.
Python checks the signed Candidate and exact pure projection. Node checks
canonical bytes, signatures, v6 successor identities, paired return identities,
donor cancellation and the fixture's committed grant/reservation funding paths:

```bash
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/expanded-handoff-vectors.json
```

The fixture uses a root funding recipient. These checks do not prove every
possible funding topology, writer arbitration, historical migration, installed
behavior or provider execution. Those require separate runtime evidence. The
fixed private key remains public test material only.

`v0.5.13/worker-profile-promotion-vectors.json` covers the signed, ordered local
promotion/register pair, the unchanged built-in registration fact and the exact
audit receipt/prior/new-registration ancestry. Python consumes the pair through
the pure projector; Node checks canonical bytes, signatures and fact identities:

```bash
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/worker-profile-promotion-vectors.json
```

These vectors establish wire interoperability, not concurrent writer arbitration,
local signer loading, installed setup or actual provider output. Profile remains
descriptive metadata. The fixed test key is public fixture material only.

`v0.5.13/task-priority-vectors.json` covers a linked signed priority history:
initial -7, update to 20, then clear to zero. Python consumes each pure projection;
Node checks canonical bytes, signatures, Candidate/fact identities and exact
predecessor and receipt ancestry:

```bash
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/task-priority-vectors.json
```

`v0.5.13/task-promotion-vectors.json` binds a signed exact multi-hold release and
its audit to the canonical declaration, placement IDs and ordered release
records. Python consumes the pure projector, and the Node consumer independently
checks canonical bytes, identities, signature and release/audit references:

```bash
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/task-promotion-vectors.json
```

These fixtures establish the selected wire behavior. They do not prove SQLite
writer arbitration, eligibility, global scheduling order or provider output.
Those require separate behavioral evidence. The fixed key is public test
material and must never be authorized in a real domain.

`v0.5.13/task-edit-vectors.json` binds an optional nested edit intent to the
existing sole signed handoff Candidate, exact source declaration and successor
text. Python consumes the pure projection; Node checks canonical signing bytes,
signature and Candidate/fact identities:

```bash
node scripts/check_task_edit_vectors.mjs
```

The fixture does not prove writer races, hold-copy boundaries, current signer
revocation, installed CLI behavior or provider execution. Those require separate
behavioral verification; the fixed key remains public fixture material only.

`v0.5.13/task-attachment-vectors.json` defines the canonical
`task.attachment.link` effect payload. Python and Node consume the same wire
vector:

```bash
node scripts/check_collaboration_vectors.mjs conformance/v0.5.13/task-attachment-vectors.json
```

This vector checks effect serialization only. Signed publication, complete
historical receipt/head membership, current authorization, Redis recovery and
exact-byte export require separate source and installed behavioral controls.
