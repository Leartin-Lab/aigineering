# Strict Fleet configuration and bounded provider responses

Status: Implemented

Fleet TOML rejects incorrectly typed strings, integers and numbers rather than
silently coercing them. Request timeouts must be finite and positive, retry counts
must be non-negative integers, and fractional timeouts are preserved when building
a Worker. Invalid configuration fails before any provider invocation.

The default HTTP transport reads at most 4 MiB plus one byte before decoding.
Oversized, invalid UTF-8 and malformed or excessively nested JSON responses become
stable non-retryable Worker errors. A caller-supplied decoded transport is outside
this wire-byte limit. No provider response body or API credential is added to
error diagnostics. The canonical recovery path remains unchanged.
