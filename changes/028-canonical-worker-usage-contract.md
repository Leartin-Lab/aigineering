# 028: Canonical Worker usage contract

Status: implemented

Worker usage metadata now has one protocol-owned allowlist and validator shared
by LLM adapters, Candidate envelopes, signed Candidate admission, failure
records, and productivity projections. Known LLM token fields preserve absent
versus explicit zero; built-in tool measurements and the existing adapter and
network fields remain available. The normal Worker runtime filters custom
metadata before it builds and signs a CandidateEnvelope; Harness and direct
CandidateEnvelope paths validate strictly. Signed Candidate payloads with
invalid usage are rejected without rewriting their bytes, using a sanitized
receipt so claim closure and recovery remain observable. Failure evidence
filters invalid usage so a secondary metadata defect cannot hide the terminal
failure. Raw metadata and usage records preserve absent usage separately from
explicit zero. Identity-only metadata does not create a usage record when no
usage amount is present. Productivity token totals aggregate known counts: an
empty aggregate is zero, and a total may be derived by adding known prompt and
completion counts. Neither an empty aggregate nor a zero total proves that the
provider charged nothing. Usage reflects only well-formed amounts explicitly
returned by a provider; it is not a complete billing ledger.

Token, duration, and byte counts are non-boolean integers from `0` through
`2^53-1`, inclusive. Usage strings are limited to 256 Unicode characters and
must encode as UTF-8; this is a character limit rather than a byte limit. The
tool `error_type` field may be the empty string for successful calls. The
existing Candidate `purpose` annotation is a separate bounded, non-empty
UTF-8 string exception; it is receipted but excluded from usage projections.

`conformance/v0.5.11/usage-vectors.json` defines the language-neutral valid,
invalid, empty, absent, and explicit-zero cases consumed by the Python tests,
including numeric and multibyte string boundaries.
