# Signed task attachment links

`aig artifact import` already stores bounded artifact-v1 attachments as signed
Assets. This change adds `aig task attachment add`, `list`, and `export` so an
operator can associate one exact committed attachment with one exact task and
copy the validated original bytes to a local path.

The association is an append-only `task.attachment.linked` fact produced by the
`task.attachment.link` injected effect and the `task.attach` capability. A new
link Candidate contains exactly one effect and binds the exact task declaration,
committed attachment fact, and that Asset's receipt/head witnesses. Task
declarations are accepted only through the direct declaration, multi-declaration,
handoff, revision, or review-request head shapes emitted by the existing
publishers, with their matching typed members. The current Candidate is
authenticated by the normal publisher and transactional writer. Historical
receipt/head/member records serve as immutable evidence of a previously accepted
Asset publication; receipt contents do not reconstruct the original Candidate
payload or prove its signature again. Unsupported or ambiguous historical
publication shapes fail closed.

List checks the complete selected task history and returns bounded safe
metadata. It does not return attachment bodies or source URIs. Export requires
an exact link, validates artifact-v1 byte length and SHA-256, and creates a new
0600 destination without overwriting an existing file or symlink. A partial
write is reported as failure and may leave the partial file created by that
attempt; a retry cannot overwrite it. Reads use an existing explicit Store and
do not write SQLite facts or materializations.

Associating an Asset does not add it to Contract inputs, labels, context, output
slots, Worker disclosure, allowance, claim state, readiness, or acceptance.
Attachment links do not reopen terminal tasks. Redis remains required for
application collaboration. If relay synchronization fails after a link commits,
the command reports the accepted Candidate and fact IDs; replaying the same
add command after Redis is restored recovers that exact link.

The public Python and Node conformance consumers use
`conformance/v0.5.13/task-attachment-vectors.json` to check the canonical effect
wire form.
