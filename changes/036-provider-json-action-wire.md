# Strict JSON action wire for compatible providers

LLM workers can opt into the OpenAI-compatible `json_object` response format
with `--response-format json_object`. This provider wire setting is separate
from `--json`, which selects CLI output formatting, and is disabled by
default.

In this mode, the adapter accepts exactly one canonical JSON action object,
rejects duplicate keys, non-finite values, extra fields, and malformed or
multiple objects, then validates the action through the existing canonical
action parser and shared Worker action compiler. JSON-valued output Assets are
serialized once by the adapter. Native tool definitions cannot be combined
with JSON object mode. Default slash-action handling remains unchanged.

Focused transport and durable-failure tests cover the JSON wire. Integrated
provider and CLI verification evidence is recorded in
`reports/070-usable-cli-stabilization-2026-10-03.md`.
