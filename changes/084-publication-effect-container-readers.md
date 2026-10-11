# Shared publication effect-container readers

Read-only retry, rejected-declaration, recovery and handoff inspection now use
one neutral reader for the `effect_types` sequence stored in a publication
receipt. It accepts ordered tuple/list values containing strings and rejects
mapping, scalar, missing and mixed-element containers before feature-specific
effect comparisons. This prevents malformed history from being interpreted as
a valid relation; it does not change Candidate commitment, writer validation or
the generic receipt/head/member proof.

The reader preserves effect order and does not whitelist effect names. Feature
owners continue to enforce their own exact effect requirements, and compound
source publications remain supported where their relation permits them.
