// Independent Node consumer of the language-neutral signed JSON boundary cases.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

const vectorPath = process.argv[2] ?? fileURLToPath(
  new URL('../conformance/v0.5.13/signed-json-boundary-vectors.json', import.meta.url),
);
const vectors = JSON.parse(fs.readFileSync(vectorPath, 'utf8'));
assert.equal(vectors.format, 'aigineering-signed-json-boundary-v1');

function validateSignedJson(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return;
  if (typeof value === 'number') {
    assert(Number.isSafeInteger(value));
    return;
  }
  if (Array.isArray(value)) {
    value.forEach(validateSignedJson);
    return;
  }
  assert.equal(typeof value, 'object');
  const normalized = new Set();
  for (const key of Object.keys(value)) {
    assert.equal(key.normalize('NFC'), key);
    assert(!normalized.has(key.normalize('NFC')));
    normalized.add(key.normalize('NFC'));
    validateSignedJson(value[key]);
  }
}

function objectFromEntries(entries) {
  const result = {};
  for (const [key, value] of entries) {
    result[key] = value?.entries ? objectFromEntries(value.entries) : value;
  }
  return result;
}

function scalarCompare(left, right) {
  const a = Array.from(left, char => char.codePointAt(0));
  const b = Array.from(right, char => char.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index++) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}

function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value !== null && typeof value === 'object') {
    return '{' + Object.keys(value).sort(scalarCompare)
      .map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  }
  if (typeof value === 'number') assert(Number.isSafeInteger(value));
  return JSON.stringify(value);
}

function validateParents(value) {
  assert(Array.isArray(value));
  for (const parent of value) assert.equal(typeof parent, 'string'), assert(parent.length > 0);
}

validateSignedJson(vectors.valid.object);
validateParents(vectors.valid.parents);
const existing = JSON.parse(fs.readFileSync(
  new URL('../conformance/v0.5.0/protocol-vectors.json', import.meta.url), 'utf8'));
const known = existing.contract_candidate;
const { id, signature, ...effective } = known.value;
const signingBytes = canonical(effective).normalize('NFC');
assert.equal(signingBytes, known.expected_signing_utf8);
assert.equal(id, 'candidate:v1:' + crypto.createHash('sha256').update(signingBytes).digest('hex'));
const publicKey = crypto.createPublicKey({
  key: Buffer.concat([
    Buffer.from('302a300506032b6570032100', 'hex'),
    Buffer.from(existing.ed25519_test_key.public_key_hex, 'hex'),
  ]),
  format: 'der', type: 'spki',
});
assert(signature.startsWith('ed25519:'));
assert(crypto.verify(
  null, Buffer.from(signingBytes), publicKey,
  Buffer.from(signature.slice('ed25519:'.length), 'base64'),
));
const boundary = vectors.candidate_vector;
const { id: boundaryId, signature: boundarySignature, ...boundaryEffective } = boundary.candidate;
const boundarySigningBytes = canonical(boundaryEffective).normalize('NFC');
assert.equal(boundarySigningBytes, boundary.expected_signing_utf8);
assert.equal(boundaryId, boundary.expected_id);
assert.equal(boundarySignature, boundary.expected_signature);
assert.equal(
  boundaryId,
  'candidate:v1:' + crypto.createHash('sha256').update(boundarySigningBytes).digest('hex'),
);
const boundaryPublicKey = crypto.createPublicKey({
  key: Buffer.concat([
    Buffer.from('302a300506032b6570032100', 'hex'),
    Buffer.from(boundary.public_key_hex, 'hex'),
  ]),
  format: 'der', type: 'spki',
});
assert(boundarySignature.startsWith('ed25519:'));
assert(crypto.verify(
  null, Buffer.from(boundarySigningBytes), boundaryPublicKey,
  Buffer.from(boundarySignature.slice('ed25519:'.length), 'base64'),
));
for (const invalid of vectors.invalid_object_keys) {
  assert.throws(() => validateSignedJson(objectFromEntries(invalid.entries)));
}
for (const invalid of vectors.invalid_parents) {
  if (invalid.raw_json) assert.throws(() => JSON.parse(invalid.raw_json));
  else assert.throws(() => validateParents(invalid.value));
}
for (const invalid of vectors.invalid_signed_values) {
  if (invalid.raw_json) assert.throws(() => JSON.parse(invalid.raw_json));
  else assert.throws(() => validateSignedJson(invalid.value));
}
const invalidContainers = {
  'scalar-string': 'record:alpha',
  bytes: 'record:alpha',
  mapping: { parent: 'record:alpha' },
  integer: 1,
};
for (const container of vectors.invalid_parent_containers) {
  assert.throws(() => validateParents(invalidContainers[container]));
}
console.log(JSON.stringify({ consumer: 'node', signed_json_boundary: 'passed' }));
