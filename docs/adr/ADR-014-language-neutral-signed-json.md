# ADR-014: Language-neutral signed Candidate JSON

Status: Accepted
Date: 2026-07-19
Related: ADR-002, ADR-011, ADR-013

## Context

Candidate identity and signature verification must agree across programming
languages. Generic JSON permits values and encodings—NaN, floating point,
oversized integers, non-string keys, and Unicode normalization variants—that
do not have one portable byte representation.

## Decision

Signed Candidate effect payloads and metadata use an interoperable JSON subset:
objects have string keys; values are objects, arrays, strings, booleans, null,
or integers within the IEEE-754 safe range. Floating-point values are rejected;
exact decimals travel as strings.

Candidate v1 rejects unknown protocol versions. Typed integer fields carried in
the signed payload, including `claim_epoch`, use the same safe-integer bound.

Canonical JSON sorts keys, uses compact separators, preserves array order and
emits Unicode without ASCII escaping. Candidate signing bytes are the NFC
normalization of the complete canonical JSON encoded as UTF-8. Candidate v1
identity hashes those same normalized bytes with SHA-256.

Versioned public vectors cover canonical JSON, fixed non-secret Ed25519 keys,
Genesis and Contract identity, typed effects, attestation, and causal allowance.
The Python implementation consumes the published files as tests.

## Consequences

- equivalent Unicode text cannot produce one Candidate ID with two signatures;
- non-Python implementations have executable expected bytes and identities;
- tool or plugin decimals must be represented as exact strings in signed data;
- adding new canonical value types requires a protocol version and vectors.

## Evidence

- `conformance/v0.5.0/protocol-vectors.json`
- `tests/conformance/test_v050_protocol_vectors.py`
