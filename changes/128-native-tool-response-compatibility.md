# Change 128: Native tool-call response compatibility

The opt-in native DeepSeek response adapter accepts provider metadata on a tool
call object and its nested function object while still validating every field
the harness consumes. Required IDs, types, function names, and argument strings
remain strict; multiple calls and malformed function shapes remain rejected.
Unconsumed metadata is discarded at the adapter boundary and does not enter the
canonical response DTO or continuation transcript.

For `finish_reason="tool_calls"`, `content` may be JSON `null` or the literal
empty string. The empty string is projected as the canonical `content=None`
value without mutating the decoded provider mapping. Nonempty text, whitespace,
and values of other types remain invalid. The call ID, function name,
arguments, and enabled reasoning are preserved for normal tool continuation.

The behavior is limited to the opt-in native DeepSeek adapter. The existing
offline `offline-v1` response envelope, request path, retry behavior, tool
authority, and harness DTO remain unchanged.
