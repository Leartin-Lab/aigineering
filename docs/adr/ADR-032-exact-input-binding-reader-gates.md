# ADR-032: Bind exact input selection and reject incompatible readers

Status: Accepted
Date: 2026-10-04

## Context

An input role historically selects Assets by name. Derived work that needs a
frozen input set cannot retain that lookup: later same-name Assets would change
its prompt. Moving inputs into context instead removes role-bound trust checks.
Older runtimes ignore unknown sensitive-policy keys, so adding a guard without
an identity and database compatibility gate would silently weaken it.

## Decision

The signed `sensitive_input_policy.input_asset_bindings` map selects exact Asset
IDs for mapped input roles. ID arrays are sorted and unique; an empty array
explicitly selects nothing. Active missing, tombstoned or wrong-role references
fail closed, with no name fallback. Unmapped roles retain existing authorized
name selection. Unused inherited bindings remain inert. The map narrows selection;
it grants neither capabilities nor permission to introduce new input roles.

A shared pure input-selection owner supplies disclosure and verification.
Original trust, signer, derivation and redaction rules continue to apply to the
selected inputs. The guard does not make sealed content promptable or convert
an assertion into independently accepted evidence.

Guard-bearing Contracts use `task:v6`, with the same canonical fields as v5 and
the complete sensitive policy bound into identity. Guard-bearing v3/v4/v5
identities are rejected; v6 requires the guard key, including an explicitly empty
map. Existing no-key Contract identities and selection semantics remain unchanged.
There is no additional Contract wire field or database column. Historical
Contracts and facts are not rewritten.

SQLite schema version 18 records reader support for the guard. The ordinary
migration transaction advances version 17 without adding tables or columns.
Older runtimes refuse to reopen the newer database. The identity domain also
makes old canonical signed declaration reject a v6 Contract independently of
its database version.

## Consequences and boundaries

Upgrading a database prevents reopening it with the previous runtime even when
it contains no v6 Contract. Keep a database backup and stop old Workers before
upgrading. The reader gate applies at normal open/reopen; it cannot stop an old
process that already holds an open connection. Mixed-version active processes
and copying raw new facts into an old database are not supported compatibility
paths.

An exact binding freezes Asset selection, not external knowledge or universal
semantic truth. Original unbound Contracts remain dynamic. Planner and tool
continuation containment still authorize new roles and preserve inherited policy;
unmapped selection is not permission to widen parent scope.
