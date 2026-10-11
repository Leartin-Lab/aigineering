# Task attachment links and exact-byte export

The local task attachment surface associates an exact committed artifact-v1
attachment Asset with an exact task. Import remains the existing signed
`artifact` ingress; association is a signed operator action requiring
`task.attach`. Redis is required for these commands.

## Import, associate and export

Initialize a fresh workspace with `aig domain init`, then import a local file:

```bash
aig artifact import evidence.pdf --name evidence --media-type application/pdf
aig task create --name analysis --output report --json
```

Copy the import result's exact `id` and the task result's `contract_id` into the
following commands as ASSET_ID and TASK_ID:

```bash
aig task attachment add TASK_ID ASSET_ID --json
aig task attachment list TASK_ID --json
aig task attachment export TASK_ID ASSET_ID --output evidence-copy.pdf --json
```

The attachment format embeds original bytes, limited to 8 MiB, with byte size
and SHA-256. Export checks the exact relation and currently available original
bytes through the shared artifact validator. It exclusively creates the explicit
output path with private permissions and refuses existing files or symlinks.
A failed partial write returns failure and can leave a partial file; choose a
fresh path after inspecting it.

An association leaves input roles, labels, Worker context, output obligations,
qualification, tools, budget and claims unchanged. To give a Worker input, use
the existing explicit immutable Contract/input mechanisms. Associations remain
operator metadata on submitted, cancelled and completed tasks as well. They
cannot reopen work.

## History and repeated operations

Default add idempotency binds the exact task/Asset pair. Repeating that add
returns the original Candidate and relation IDs. Keep those IDs when retrying;
reimporting a file can produce a different Asset because import metadata is part
of its identity. An explicit different `--idempotency-key` may create another
attributed relation. Reusing a key for changed intent fails. Current operator
authorization still applies to replay.

List defaults to 20 relations and accepts `--limit` from 1 to 100. Continue a
returned `next_cursor` with the same task, Store and limit. The cursor fixes the
original domain/revision boundary; later links do not enter that page sequence.
Readers validate the selected history before pagination. Metadata includes
exact relation/Asset/Candidate IDs, attribution, name, media type, size and hash;
embedded bytes and source URI are omitted.

Historical links remain in the signed history if content later becomes
unavailable. Export fails for unavailable, non-original, malformed or unlinked
content. The relation does not grant remote access or establish accepted business
evidence. Original publication witnesses are checked against accepted durable
receipt/head/member history; this local inspection does not reconstruct every
original Candidate signature from its receipt alone.

## Select a Store and recover relay synchronization

All three attachment commands accept `--store PATH`, defaulting to
`.aig/store.db`. List/export copy a read-only snapshot and leave the source
Store unchanged. A missing selected Store fails; no default Store is created or
substituted. For a custom Store, select the matching local actor identity using
the usual `AIG_ACTOR_KEY_FILE` configuration. `aig -C WORKSPACE` remains the
ordinary workspace selection for import and task creation.

If add commits but relay synchronization fails, its nonzero
`redis_sync_pending` result reports the retained accepted Candidate/fact IDs.
Restore Redis, then repeat the exact add with the same Store, pair and key. The
same publication owner synchronizes the existing records and returns their
original identities. Precommit or unknown failures cannot claim retained
accepted facts. Listing and copying files never repair task facts or reset
claims.

This slice adds association/list/export over the existing attachment format.
Unlink, retention and larger external blob storage remain later work. See
[the attachment change](../../changes/065-signed-task-attachment-links.md) and
[the architecture decision](../adr/ADR-045-signed-task-attachment-links.md).
