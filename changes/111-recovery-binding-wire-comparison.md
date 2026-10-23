# Compare recovery bindings in canonical wire form

Status: Implemented development increment
Date: 2026-10-23

Exact recovery publication recognition compared the immutable declaration
payload directly with a newly serialized Contract. A v6 input binding is frozen
as a tuple in the stored RuntimeRecord and serialized as a JSON list, so an
otherwise exact accepted publication was reported as a materialization
mismatch. The history proof now recursively thaws both values before comparing
them. This normalizes container representation while retaining exact keys,
values and sequence order.

The recovery proof still reconstructs the Candidate from its accepted receipt,
checks the exact effect order, idempotency key and causal parents, validates the
receipt, committed head and declaration member, reprojects the Contract and
context Asset, and verifies both materialized objects. No receipt, authority,
funding, projection or publication-member check was relaxed.

Tests cover a valid signed v6 declaration with pinned input bindings and an
actual signed system-tool failure recovery. The v6 declaration's frozen binding
is asserted to be a tuple while its wire representation is a list; the accepted
publication must still match exact recovery history. The system-tool case also
checks that the recovered Contract preserves its source bindings and that its
publication is recognized through the same proof owner.
