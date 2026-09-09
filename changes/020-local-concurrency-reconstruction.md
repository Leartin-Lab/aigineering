# Local concurrency and trace reconstruction

Status: implemented in the v0.5.10 development line.

## Problem

Concurrent recovery can append multiple immutable trace records for the same
semantic trace ID with different timestamps. Live SQLite trace insertion keeps
the first observation, but reconstruction selected the last record. This changed
the materialization digest after rebuild. Ubuntu CI exposed the recovery case
on Python 3.11 and 3.13 in v0.5.8 and again on 3.13 in v0.5.9.

Local Fleet slots could also race while provisioning a shared plugin signing key.
An existence check followed by exclusive creation did not synchronize creators,
and publishing the path before writing the complete key allowed partial reads.

## Changes

Trace reconstruction preserves the first committed observation in revision order,
matching live insertion. Repeated IDs with differing semantic payloads fail
closed. Immutable records and existing wire identities are unchanged.

Local key publication writes and flushes a private temporary file before atomically
linking it into its final path without replacing an existing key. Losing creators
load the winner's complete key. The local identity adapter retains this filesystem
responsibility; the commitment kernel is unchanged.

## Evidence

The Fleet recovery test synchronizes independent connections before recovery
publication and checks complete trace equality as well as the rebuild digest.
The test fails against the previous rebuild implementation and passes with the
first-observation rule. Local identity tests exercise shared publisher identities.

This identifies the observed CI mismatch. The older v0.5.6 diagnostic lost its
pre-rebuild evidence; its cause cannot be conclusively identified retroactively.
