// Independent Node consumer for the signed task-edit wire vector.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

const path = process.argv[2] ?? fileURLToPath(
  new URL('../conformance/v0.5.13/task-edit-vectors.json', import.meta.url),
);
const vector = JSON.parse(fs.readFileSync(path, 'utf8'));

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

function recordIdentity(record) {
  const effective = {
    causal_parents: record.causal_parents,
    payload: record.payload,
    record_type: record.record_type,
    schema_version: record.schema_version,
  };
  return 'record:' + crypto.createHash('sha256')
    .update(canonical(effective).normalize('NFC'), 'utf8').digest('hex');
}

assert.equal(vector.format, 'aigineering-task-edit-v1');
const candidate = vector.candidate.value;
const { id, signature, ...signedValue } = candidate;
const bytes = Buffer.from(canonical(signedValue).normalize('NFC'), 'utf8');
assert.equal(bytes.toString('utf8'), vector.candidate.expected_signing_utf8);
assert.equal(id, 'candidate:v1:' + crypto.createHash('sha256')
  .update(canonical(signedValue).normalize('NFC')).digest('hex'));
const root = vector.genesis.root_keys.find(key => key.key_id === candidate.key_id);
assert(root);
const publicKey = crypto.createPublicKey({
  key: Buffer.concat([
    Buffer.from('302a300506032b6570032100', 'hex'),
    Buffer.from(root.public_key, 'hex'),
  ]),
  format: 'der',
  type: 'spki',
});
assert(crypto.verify(null, bytes, publicKey, Buffer.from(signature.split(':')[1], 'base64')));
assert.deepEqual(Object.keys(candidate.effects[0].payload.edit).sort(scalarCompare), [
  'body', 'source_assignment_id', 'source_hold_ids', 'title',
].sort(scalarCompare));
assert.deepEqual(candidate.effects[0].payload.edit, vector.edit);
assert.deepEqual(candidate.causal_parents, [
  vector.source_contract.id,
  vector.source_declaration.id,
]);

const [terminal, successorDeclaration, handoff] = vector.records;
assert.equal(vector.records.length, 3);
assert.equal(new Set(vector.records.map(record => record.id)).size, 3);
for (const record of vector.records) {
  assert.equal(record.id, recordIdentity(record), `${record.record_type} identity`);
}
assert.equal(vector.source_declaration.id, recordIdentity(vector.source_declaration));
assert.equal(vector.receipt.id, recordIdentity(vector.receipt));
assert.equal(vector.source_declaration.record_type, 'contract.declared');
assert.deepEqual(vector.source_declaration.payload.contract, vector.source_contract);
assert.deepEqual(vector.source_declaration.causal_parents, []);
assert.equal(vector.receipt.record_type, 'candidate.received');
assert.equal(vector.receipt.payload.candidate_id, candidate.id);
assert.equal(vector.receipt.payload.domain_id, candidate.domain_id);
assert.equal(vector.receipt.payload.actor_id, candidate.actor_id);
assert.equal(vector.receipt.payload.key_id, candidate.key_id);
assert.equal(vector.receipt.payload.signature, signature);
assert.deepEqual(vector.receipt.payload.effect_types, ['task.handoff']);
assert.deepEqual(vector.receipt.causal_parents, candidate.causal_parents);

assert.equal(terminal.record_type, 'lifecycle.terminal');
assert.deepEqual(terminal.payload, {
  contract_id: vector.source_contract.id,
  terminal: 'cancelled',
  reason: candidate.effects[0].payload.reason,
  actor_id: candidate.actor_id,
});
assert.deepEqual(terminal.causal_parents, [vector.receipt.id]);
assert.equal(successorDeclaration.record_type, 'contract.declared');
assert.equal(successorDeclaration.payload.candidate_id, candidate.id);
assert.deepEqual(successorDeclaration.payload.contract, vector.successor);
assert.deepEqual(successorDeclaration.causal_parents, [vector.receipt.id]);
assert.equal(vector.successor.parent_id, vector.source_contract.id);
assert.deepEqual(vector.successor.outputs, vector.source_contract.outputs);
assert.equal(vector.successor.name, vector.edit.title);
assert.equal(vector.successor.description, vector.edit.body);

assert.equal(handoff.record_type, 'task.handed_off');
assert.equal(handoff.payload.candidate_id, candidate.id);
assert.equal(handoff.payload.actor_id, candidate.actor_id);
assert.equal(handoff.payload.key_id, candidate.key_id);
assert.equal(handoff.payload.contract_id, vector.source_contract.id);
assert.equal(handoff.payload.successor_id, vector.successor.id);
assert.equal(handoff.payload.budget, vector.successor.budget);
assert.equal(handoff.payload.reason, candidate.effects[0].payload.reason);
assert.equal(handoff.payload.edit.source_declaration_id, vector.source_declaration.id);
assert.equal(handoff.payload.edit.successor_declaration_id, successorDeclaration.id);
assert.deepEqual(handoff.payload.edit.changed_fields, ['title', 'body']);
assert.deepEqual(handoff.payload.edit.source_hold_ids, []);
assert.deepEqual(handoff.payload.edit.hold_mapping, []);
assert.equal(handoff.payload.edit.source_assignment_id, '');
assert.deepEqual(handoff.causal_parents, [
  vector.receipt.id,
  terminal.id,
  successorDeclaration.id,
  vector.source_declaration.id,
]);
console.log(JSON.stringify({ consumer: 'node', candidate_id: id, signature: 'passed' }));
