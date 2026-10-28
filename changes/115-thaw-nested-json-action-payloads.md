# Preserve nested JSON action payloads in JSON response mode

Status: Implemented development increment
Date: 2026-10-23

JSON response mode now recursively thaws a validated non-exec action payload
before canonical serialization. Actions such as attestations, tool calls, and
parallel tool calls can retain nested objects and arrays after the canonical
action parser freezes their payload.

The wire still passes the same strict JSON duplicate-key and constant checks,
supported action validation, and canonical serialization. Malformed wire or
unsupported actions remain rejected as `malformed_json_action`.
