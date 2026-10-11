# ADR-027: Require Redis for operational task collaboration

Status: Accepted for the local development implementation
Date: 2026-10-03
Supersedes: ADR-016's optional application configuration and automatic fallback

## Context

The optional query projection did not provide a common operational feed or
participate in Worker waiting. Task discovery and repeated SQLite polling were
separate interface behaviors. A usable collaboration CLI needs a default
component for indexed discovery, event cursors and Worker wakeups.

## Decision

Redis is a required application runtime component and a base Python dependency.
Normal CLI operations and HTTP Store requests preflight it. The default address
is local Redis; an explicit environment variable selects another deployment.
Application queries fail visibly rather than silently falling back to SQLite.
Selected offline CLI inspection commands remain available for incident diagnosis.

SQLite immutable RuntimeRecords supply a replayable event outbox. An application
relay appends revision-addressed metadata to a domain/schema-scoped Redis Stream.
Lua atomically appends an event and advances its watermark. Atomic state reads,
conditional repair and bounded retry handle concurrent relays and rebuilds.
The feed carries record references and typed task references, never copied
descriptions, prompt content, Candidate payloads or credentials. Redis also
serves fact-derived read indexes. Consumers use notifications to wake and then
claim through the existing SQLite protocol.

Commitment, claim fencing, allowance, authority, acceptance and terminal facts
remain in one SQLite transaction. Redis is independently repairable and does not
decide whether a Candidate was accepted. Preflight reduces outage windows;
post-commit relay failure reports retained facts and pending synchronization.
It must never relabel an accepted Candidate as a rejection. Reconnection replays
the SQLite records rather than resetting claimed work or republishing a task.

## Alternatives

Keeping Redis optional leaves operational collaboration split across interfaces.
Making Redis a claim or fact authority adds a second transactional owner and an
unsolved cross-store atomicity requirement. A required application service with
a replayable source provides collaboration without changing the signed protocol.

## Consequences

Installations must run Redis. Availability is required for normal execution,
while recovery inspection can explicitly use local SQLite snapshots. Operators
must treat Redis as application infrastructure, including its read index data;
its rebuildability does not make it an optional performance cache. The low-level
adapter's compatibility fallback remains available to library callers only when
explicitly selected; public application defaults are strict.

The interface takes inspiration from the discovery, observation and lifecycle
commands in [Hermes Kanban](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md).
Its mutable card states and dispatcher are not imported into this runtime.
