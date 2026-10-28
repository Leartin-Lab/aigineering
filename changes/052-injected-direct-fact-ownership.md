# Injected direct-fact declarations and selective ownership

Status: Development implementation; bounded integration verified.

Trusted injected effect handlers can declare a complete set of directly returned
fact types through `EffectHandler.direct_output_fact_types`. An explicit empty
set permits no direct facts. `reserved_fact_types` selects exclusive owners
within the frozen handler mapping; duplicate owners are rejected, and another
effect cannot emit a reserved fact. The existing assignment metadata remains
unchanged. In-tree operational registries use strict direct declarations;
external typed and tuple handlers with the default `None` retain trusted
compatibility behavior.

Projectors must return an `EffectProjection` whose records are a tuple of valid
canonical `RuntimeRecord` objects. Invalid containers, members or record
identities reject durably instead of escaping as an unrecorded attribute error.
Legacy membership compatibility does not relax this return-shape requirement.

Single-effect compound replay can reproduce its complete original committed
head only after exact receipt, head, ordered membership and record equality
checks. Only generic allowance grant/reservation and asset materialization
co-effects can sit outside the strict direct set; reserved ownership still
applies to every returned record. Multi-effect projections receive no such
exception. This preserves historical replay without widening fresh authority.

The existing effect-owner fingerprint and SQLite writer re-projection carry the
decision. No Store table, schema floor or wire format changes. This constrains
trusted handler configuration; it does not sandbox Python callbacks or make
legacy extensions safe to load from untrusted sources.

See [ADR-040](../docs/adr/ADR-040-injected-direct-fact-ownership.md).
Strict ownership declarations, legacy compatibility and compound replay require
regression coverage; they do not establish provider quality.
