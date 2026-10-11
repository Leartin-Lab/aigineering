# Submitted Worker task views

Canonical submitted claims now remain visible as `submitted` across task status,
boards, waits, and run guidance. When a submitted task has a complete set of
committed business outputs eligible under its frozen independent policy, the
view exposes their exact IDs in `qualification_pending_outputs` and reports an
independent-qualification blocker instead of a false missing-recovery risk.
Already qualified output slots are omitted using their exact canonical
`output.qualified` facts; remaining unqualified outputs keep their exact Asset
references while any declared output is still awaiting review.
This is a read-only projection; it does not qualify output, reset claims, or
change recovery behavior for incomplete output or legacy orphan projections.

Validation: signed Worker fixtures cover pending qualification, same-name
foreign noise, incomplete output, legacy orphan projection, state precedence,
board filtering, and reopen/rebuild preservation. Full suite and installed
artifact verification remain with integration.
