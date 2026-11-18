# Historical task attachments

New attachment-link publication has been removed. Import files as ordinary
Assets with `aig artifact import` and bind exact Assets to task inputs when
creating work. An imported file does not qualify business conclusions.

Existing signed attachment links remain readable:

```bash
aig task attachment list TASK_ID --json
aig task attachment export TASK_ID ASSET_ID --output evidence-copy.pdf --json
```

Readers verify the original receipt/head/member history and exact Task and Asset
materialization. Export verifies the pinned bytes and creates a fresh private
file; existing files, symlinks, unavailable or changed content are refused.
These commands require Redis and accept `--store PATH`. They never modify task
requirements, qualification, claims or history. Restore Redis and synchronize
accepted facts before resuming a failed relay.
