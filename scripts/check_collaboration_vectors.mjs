// Independent Node consumer of the public signed-comment conformance vector.
// Uses only Node built-ins; it does not import the Python implementation.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

const vectorPath = process.argv[2] ?? fileURLToPath(
  new URL('../conformance/v0.5.13/task-comment-vectors.json', import.meta.url),
);
const vectors = JSON.parse(fs.readFileSync(vectorPath, 'utf8'));

function scalarCompare(left, right) {
  const a = Array.from(left, c => c.codePointAt(0));
  const b = Array.from(right, c => c.codePointAt(0));
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
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

function digest(value) {
  return crypto.createHash('sha256').update(canonical(value).normalize('NFC')).digest('hex');
}

if (vectors.format === 'aigineering-task-attachment-v1') {
  assert.equal(canonical(vectors.effect), vectors.expected_canonical_json);
  assert.equal(vectors.effect.effect_type, 'task.attachment.link');
  console.log(JSON.stringify({ consumer: 'node', effect_wire: 'passed' }));
  process.exit(0);
}

function verifyBoundContract(contract) {
  assert(contract.id.startsWith('task:v6:'));
  assert(Object.hasOwn(contract.sensitive_input_policy, 'input_asset_bindings'));
  const values = {};
  for (const key of ['activation', 'budget', 'description', 'name', 'origin',
    'parent_id', 'sensitive_input_policy']) values[key] = contract[key];
  for (const key of ['inputs', 'outputs', 'labels', 'tool_scope', 'context_asset_ids',
    'worker_capabilities', 'worker_pools', 'delegation_capabilities', 'delegation_pools']) {
    values[key] = [...contract[key]].sort(scalarCompare);
  }
  values.minting_authority = contract.minting_authority.map(
    value => value.split(contract.id).join('{contract_id}'),
  ).sort(scalarCompare);
  if (contract.acceptance_policy !== null) values.acceptance_policy = contract.acceptance_policy;
  assert.equal(contract.id, 'task:v6:' + digest(values));
}

assert.equal(canonical(vectors.canonical_json.input), vectors.canonical_json.expected);
const publicKey = crypto.createPublicKey({
  key: Buffer.concat([
    Buffer.from('302a300506032b6570032100', 'hex'),
    Buffer.from(vectors.ed25519_test_key.public_key_hex, 'hex'),
  ]),
  format: 'der', type: 'spki',
});
const effectVectors = vectors.effect_vectors ?? (vectors.effect_vector ? [vectors.effect_vector] :
  (vectors.candidate_vector ? [vectors.candidate_vector] : (vectors.candidate ? [{
  candidate: vectors.candidate, records: vectors.records, receipt: vectors.receipt,
}] : [{
  candidate: vectors.comment_candidate, records: [vectors.comment_record],
}])));
let priorityHead = '';
for (const item of effectVectors) {
  const { id, signature, ...effective } = item.candidate.value;
  if (vectors.format === 'aigineering-task-dependency-v1') {
    assert.equal(effective.effects.length, 1);
    assert(['task.dependency.link', 'task.dependency.unlink'].includes(
      effective.effects[0].effect_type));
    assert.equal(item.records.length, 1);
    const [record] = item.records;
    assert.equal(record.schema_version, 1);
    assert(record.causal_parents.includes(`record:${digest({
      causal_parents: [],
      payload: {
        actor_id: effective.actor_id,
        candidate_id: id,
        domain_id: effective.domain_id,
        effect_types: effective.effects.map(effect => effect.effect_type),
        key_id: effective.key_id,
        metadata: {},
        signature,
        signature_kind: effective.signature_kind,
      },
      record_type: 'candidate.received',
      schema_version: 1,
    })}`));
    const payload = record.payload;
    assert.equal(payload.actor_id, effective.actor_id);
    assert.equal(payload.key_id, effective.key_id);
    assert.equal(payload.candidate_id, id);
    if (effective.effects[0].effect_type === 'task.dependency.link') {
      assert.equal(record.record_type, 'claim.constraint.placed');
      assert.deepEqual(Object.keys(payload).sort(scalarCompare), [
        'actor_id', 'candidate_id', 'contract_id', 'key_id', 'predicate',
        'prerequisite_contract_id', 'reason', 'required_terminal',
      ].sort(scalarCompare));
      assert.equal(payload.predicate, 'prerequisite_terminal');
      assert.equal(payload.required_terminal, 'complete');
      assert.notEqual(payload.contract_id, payload.prerequisite_contract_id);
    } else {
      assert.equal(record.record_type, 'claim.constraint.released');
      assert.equal(effective.effects[0].payload.constraint_id, payload.constraint_id);
      assert(record.causal_parents.includes(payload.constraint_id));
      const placement = vectors.effect_vectors[0].records[0];
      assert.equal(placement.id, payload.constraint_id);
      for (const key of ['contract_id', 'prerequisite_contract_id', 'predicate',
        'required_terminal']) assert.equal(payload[key], placement.payload[key]);
    }
  } else if (vectors.format === 'aigineering-task-assignment-v1') {
    assert.equal(effective.effects.length, 1);
    assert.equal(effective.effects[0].effect_type, 'task.assign');
    assert.equal(item.records.length, 1);
    const [record] = item.records;
    assert.equal(record.record_type, 'claim.assignment.changed');
    assert.deepEqual(Object.keys(record.payload).sort(scalarCompare), [
      'actor_id', 'candidate_id', 'contract_id', 'key_id',
      'previous_assignment_id', 'reason', 'worker_id',
    ].sort(scalarCompare));
    const payload = record.payload;
    assert.equal(payload.actor_id, effective.actor_id);
    assert.equal(payload.key_id, effective.key_id);
    assert.equal(payload.candidate_id, id);
    assert.equal(payload.contract_id, effective.effects[0].payload.contract_id);
    assert.equal(payload.previous_assignment_id, '');
    assert.equal(payload.worker_id, effective.effects[0].payload.worker_id);
    const receipt = item.receipt;
    assert.equal(receipt.record_type, 'candidate.received');
    assert.equal(receipt.payload.candidate_id, id);
    assert(record.causal_parents.includes(receipt.id));
    assert(record.causal_parents.includes(vectors.context_records.find(
      value => value.record_type === 'contract.declared').id));
    assert(record.causal_parents.includes(vectors.context_records.find(
      value => value.record_type === 'worker.registered').id));
  } else if (vectors.format === 'aigineering-worker-profile-promotion-v1') {
    assert.equal(effective.effects.length, 2);
    const [promotion, registrationEffect] = effective.effects;
    assert.equal(promotion.effect_type, 'worker.profile.promote');
    assert.equal(registrationEffect.effect_type, 'worker.register');
    assert.deepEqual(Object.keys(promotion.payload).sort(scalarCompare), [
      'previous_registration_id', 'registration',
    ]);
    const expectedRegistration = { ...promotion.payload.registration };
    assert.deepEqual(registrationEffect.payload.registration, expectedRegistration);
    assert.deepEqual(Object.keys(expectedRegistration).sort(scalarCompare), [
      'actor_id', 'capabilities', 'capacity', 'enabled', 'key_id', 'pools',
      'profile_id', 'version', 'worker_id',
    ]);
    assert.equal(item.records.length, 2);
    const [audit, registered] = item.records;
    assert.equal(audit.record_type, 'worker.profile.promoted');
    assert.equal(registered.record_type, 'worker.registered');
    assert.deepEqual(Object.keys(audit.payload).sort(scalarCompare), [
      'actor_id', 'candidate_id', 'key_id', 'previous_registration_id',
      'previous_version', 'profile_id', 'registration_id', 'version', 'worker_id',
    ]);
    assert.equal(audit.payload.candidate_id, id);
    assert.equal(audit.payload.previous_registration_id,
      promotion.payload.previous_registration_id);
    assert.equal(audit.payload.registration_id, registered.id);
    assert.equal(audit.payload.previous_version, vectors.prior_registration.version);
    assert.equal(audit.payload.version, expectedRegistration.version);
    assert.equal(audit.payload.profile_id, expectedRegistration.profile_id);
    assert.equal(audit.payload.actor_id, expectedRegistration.actor_id);
    assert.equal(audit.payload.key_id, expectedRegistration.key_id);
    assert.notEqual(effective.actor_id, audit.payload.actor_id);
    assert.notEqual(effective.key_id, audit.payload.key_id);
    assert.deepEqual(registered.payload, {
      ...expectedRegistration,
      registered_by: effective.actor_id,
    });
    assert.deepEqual(registered.causal_parents, [item.receipt.id]);
    assert.deepEqual(audit.causal_parents, [
      item.receipt.id,
      promotion.payload.previous_registration_id,
      registered.id,
    ]);
    assert.equal(item.receipt.record_type, 'candidate.received');
    assert.equal(item.receipt.payload.candidate_id, id);
    const { registered_by, ...priorRegistration } = vectors.prior_registration;
    assert(registered_by);
    assert.deepEqual(promotion.payload.registration, {
      ...priorRegistration,
      profile_id: expectedRegistration.profile_id,
      version: expectedRegistration.version,
    });
  } else if (vectors.format === 'aigineering-task-priority-v1') {
    assert.equal(effective.effects.length, 1);
    const [priorityEffect] = effective.effects;
    assert.equal(priorityEffect.effect_type, 'task.priority.set');
    assert.deepEqual(Object.keys(priorityEffect.payload).sort(scalarCompare), [
      'contract_id', 'previous_priority_id', 'priority', 'reason',
    ]);
    assert(Number.isInteger(priorityEffect.payload.priority));
    assert(priorityEffect.payload.priority >= -100 && priorityEffect.payload.priority <= 100);
    assert.equal(priorityEffect.payload.previous_priority_id, priorityHead);
    assert.equal(item.records.length, 1);
    const [record] = item.records;
    assert.equal(record.record_type, 'task.priority.changed');
    assert.deepEqual(Object.keys(record.payload).sort(scalarCompare), [
      'actor_id', 'candidate_id', 'contract_id', 'key_id',
      'previous_priority_id', 'priority', 'reason',
    ]);
    assert.equal(record.payload.actor_id, effective.actor_id);
    assert.equal(record.payload.key_id, effective.key_id);
    assert.equal(record.payload.candidate_id, id);
    assert.equal(record.payload.contract_id, priorityEffect.payload.contract_id);
    assert.equal(record.payload.priority, priorityEffect.payload.priority);
    assert.equal(record.payload.previous_priority_id, priorityHead);
    const declarationId = vectors.context_records.find(
      value => value.record_type === 'contract.declared').id;
    assert.deepEqual(record.causal_parents, [
      item.receipt.id,
      declarationId,
      ...(priorityHead ? [priorityHead] : []),
    ]);
    priorityHead = record.id;
  } else if (vectors.format === 'aigineering-task-promotion-v1') {
    assert.equal(effective.effects.length, 1);
    const [promotion] = effective.effects;
    assert.equal(promotion.effect_type, 'task.promote');
    assert.deepEqual(Object.keys(promotion.payload).sort(scalarCompare), [
      'contract_id', 'hold_ids', 'reason',
    ]);
    const holdIds = promotion.payload.hold_ids;
    assert(Array.isArray(holdIds) && holdIds.length >= 1 && holdIds.length <= 64);
    assert.deepEqual(holdIds, [...new Set(holdIds)].sort(scalarCompare));
    const declaration = vectors.context_records.find(
      record => record.record_type === 'contract.declared',
    );
    assert(declaration);
    assert.deepEqual(effective.causal_parents, [declaration.id, ...holdIds]);
    assert.equal(item.records.length, holdIds.length + 1);
    const releases = item.records.slice(0, holdIds.length);
    const audit = item.records.at(-1);
    assert.deepEqual(releases.map(record => record.payload.hold_id), holdIds);
    for (const [index, release] of releases.entries()) {
      assert.equal(release.record_type, 'claim.hold.released');
      assert.deepEqual(Object.keys(release.payload).sort(scalarCompare), [
        'actor_id', 'candidate_id', 'contract_id', 'hold_id', 'key_id', 'reason',
      ]);
      assert.equal(release.payload.actor_id, effective.actor_id);
      assert.equal(release.payload.key_id, effective.key_id);
      assert.equal(release.payload.candidate_id, id);
      assert.equal(release.payload.contract_id, promotion.payload.contract_id);
      assert.equal(release.payload.reason, promotion.payload.reason);
      assert.deepEqual(release.causal_parents, [item.receipt.id, holdIds[index]]);
    }
    assert.equal(audit.record_type, 'task.promoted');
    assert.deepEqual(Object.keys(audit.payload).sort(scalarCompare), [
      'actor_id', 'candidate_id', 'contract_id', 'hold_ids', 'key_id', 'reason',
      'release_record_ids',
    ]);
    assert.deepEqual(audit.payload.hold_ids, holdIds);
    assert.deepEqual(audit.payload.release_record_ids, releases.map(record => record.id));
    assert.equal(audit.payload.actor_id, effective.actor_id);
    assert.equal(audit.payload.key_id, effective.key_id);
    assert.equal(audit.payload.candidate_id, id);
    assert.deepEqual(audit.causal_parents, [
      item.receipt.id, ...holdIds, ...releases.map(record => record.id),
    ]);
  }
  const bytes = Buffer.from(canonical(effective).normalize('NFC'), 'utf8');
  assert.equal(bytes.toString('utf8'), item.candidate.expected_signing_utf8);
  assert.equal(id, 'candidate:v1:' + digest(effective));
  assert(crypto.verify(null, bytes, publicKey, Buffer.from(signature.split(':')[1], 'base64')));
  for (const record of item.records) {
    assert.equal(record.id, 'record:' + digest({
      causal_parents: record.causal_parents,
      payload: record.payload,
      record_type: record.record_type,
      schema_version: record.schema_version,
    }));
  }
  if (item.contract?.id.startsWith('task:v6:')) verifyBoundContract(item.contract);
  if (vectors.format === 'aigineering-expanded-handoff-v1') {
    const prior = new Map(item.context.runtime_records.map(record => [record.id, record]));
    const projected = new Map(item.records.map(record => [record.id, record]));
    const contracts = new Map(item.context.contracts.map(contract => [contract.id, contract]));
    const balance = (contractId) => {
      let value = 0;
      for (const record of prior.values()) {
        const payload = record.payload;
        if (record.record_type === 'allowance.granted' && payload.contract_id === contractId) value += payload.amount;
        if (record.record_type === 'allowance.reserved' && payload.source_contract_id === contractId) value -= payload.amount;
        if (record.record_type === 'allowance.extinguished' && payload.contract_id === contractId) value -= payload.amount;
        if (record.record_type === 'allowance.returned') {
          if (payload.source_contract_id === contractId) value += payload.amount;
          if (payload.from_contract_id === contractId) value -= payload.amount;
        }
      }
      return value;
    };
    let incoming = 0;
    for (const record of item.records.filter(value => value.record_type === 'allowance.returned')) {
      const payload = record.payload;
      assert(Number.isSafeInteger(payload.amount) && payload.amount > 0);
      assert.equal(payload.candidate_id, id);
      assert.equal(payload.return_id, 'return:' + digest({ candidate_id: id,
        donor_contract_id: payload.from_contract_id, recipient_contract_id: payload.source_contract_id }));
      assert.deepEqual(record.causal_parents, [payload.receipt_id, payload.cancellation_record_id,
        ...payload.funding_path_record_ids]);
      const cancelled = projected.get(payload.cancellation_record_id);
      assert.equal(cancelled.record_type, 'lifecycle.terminal');
      assert.equal(cancelled.payload.terminal, 'cancelled');
      assert.equal(cancelled.payload.contract_id, payload.from_contract_id);
      assert(cancelled.causal_parents.includes(payload.receipt_id));
      let donor = contracts.get(payload.from_contract_id);
      const recipient = contracts.get(payload.source_contract_id);
      assert.notEqual(donor.id, recipient.id);
      const funding = payload.funding_path_record_ids.map(recordId => {
        const fact = prior.get(recordId);
        assert(fact);
        assert.equal(fact.id, 'record:' + digest({ causal_parents: fact.causal_parents,
          payload: fact.payload, record_type: fact.record_type, schema_version: fact.schema_version }));
        return fact;
      });
      // This fixture uses a root recipient; the source grant is explicit proof.
      assert.equal(recipient.parent_id, null);
      assert.equal(funding[0].record_type, 'allowance.granted');
      assert.equal(funding[0].payload.contract_id, recipient.id);
      assert.equal(funding[0].payload.amount, recipient.budget);
      let offset = 1;
      do {
        const reservation = funding[offset++];
        const grant = funding[offset++];
        assert.equal(reservation.record_type, 'allowance.reserved');
        assert.equal(grant.record_type, 'allowance.granted');
        assert.equal(reservation.payload.child_contract_id, donor.id);
        assert.equal(reservation.payload.source_contract_id, donor.parent_id);
        assert.equal(grant.payload.contract_id, donor.id);
        assert.equal(grant.payload.source_contract_id, donor.parent_id);
        assert.equal(reservation.payload.amount, donor.budget);
        assert.equal(grant.payload.amount, donor.budget);
        assert.deepEqual(grant.causal_parents, [reservation.id]);
        assert([...prior.values()].some(head => head.record_type === 'candidate.committed'
          && head.payload.committed_record_ids.includes(reservation.id)
          && head.payload.committed_record_ids.includes(grant.id)));
        donor = contracts.get(donor.parent_id);
        assert(donor);
      } while (donor.id !== recipient.id);
      assert.equal(offset, funding.length);
      if (prior.has(record.id)) {
        assert.deepEqual(prior.get(record.id).payload, payload);
      } else {
        assert(payload.amount <= balance(payload.from_contract_id));
        incoming += payload.amount;
      }
    }
    if (!prior.has(item.records.find(record => record.record_type === 'task.handed_off').id)) {
      assert(item.contract.budget <= balance(item.contract.parent_id) + incoming);
    }
  }
}
for (const item of vectors.successor_override_vectors ?? []) {
  const { id, signature, ...effective } = item.candidate.value;
  assert.equal(effective.effects.length, 1);
  const effect = effective.effects[0];
  assert.equal(effect.effect_type, 'task.handoff');
  assert.deepEqual(effect.payload.assignment_override, item.assignment_override);
  assert.deepEqual(Object.keys(item.assignment_override).sort(scalarCompare), [
    'source_assignment_id', 'worker_id',
  ]);
  assert.equal(item.assignment_override.worker_id, item.name === 'clear'
    ? '' : item.assignment_override.worker_id);
  const bytes = Buffer.from(canonical(effective).normalize('NFC'), 'utf8');
  assert.equal(bytes.toString('utf8'), item.candidate.expected_signing_utf8);
  assert.equal(id, 'candidate:v1:' + digest(effective));
  assert(crypto.verify(null, bytes, publicKey, Buffer.from(signature.split(':')[1], 'base64')));
}
for (const item of vectors.contract_identity_vectors ?? []) verifyBoundContract(item.value);
console.log(JSON.stringify({ signing_bytes: effectVectors.length ? 'passed' : 'not_applicable',
  signature: effectVectors.length ? 'passed' : 'not_applicable',
  candidate_id: effectVectors.length ? 'passed' : 'not_applicable',
  runtime_record_id: effectVectors.length ? 'passed' : 'not_applicable', consumer: 'node',
  effect_vectors: effectVectors.length,
  successor_override_vectors: (vectors.successor_override_vectors ?? []).length,
  paired_allowance_vectors: vectors.format === 'aigineering-expanded-handoff-v1' ? effectVectors.length : 0,
  projected_bound_contracts: effectVectors.filter(item => item.contract?.id.startsWith('task:v6:')).length,
  bound_contract_vectors: (vectors.contract_identity_vectors ?? []).length }));
