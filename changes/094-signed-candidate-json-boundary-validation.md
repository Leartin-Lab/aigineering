# Signed Candidate JSON boundary validation

Candidate v1 admission now requires causal parents to be arrays of non-empty
strings and signed JSON object keys to be already NFC with no normalized-key
collisions. The shared validator covers effect payloads, Candidate metadata and
definition assertion evidence. Signing bytes, IDs, valid Unicode values,
historical facts and storage schema are unchanged. Malformed historical facts
remain readable and rebuildable; malformed new envelopes fail before projection
or commitment.

Python and the independent Node consumer verify a fixed-key v0.5.13 Candidate
vector with expected signing bytes, ID and signature, and consume shared valid
and invalid boundary cases. These checks establish wire-domain conformance, not
a semantic task-state bypass or an attack using canonically equivalent keys.
Raw JSON texts with repeated identical member names remain outside this
mapping-level validation boundary.
