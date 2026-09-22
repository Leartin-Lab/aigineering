# Worker failure usage evidence

When a provider response includes well-formed token usage but fails during
response extraction, tool mapping, or Worker Candidate envelope compilation,
the runtime now carries the bounded usage fields into the existing durable
worker-failure trace and RuntimeRecord. `task status` and `task audit` can
therefore report the observed usage for failed attempts after restart and
rebuild.

Only `prompt_tokens`, `completion_tokens`, `total_tokens`, `model`, `provider`,
and `worker_profile` are retained. Token counts must be non-negative
interoperable integers and identity strings are bounded UTF-8 strings;
unknown or malformed metadata is dropped. Missing usage remains absent and is
never represented as zero.

Validation: `tests/test_llm_worker.py`, `tests/test_worker_routing.py`, and
`tests/test_worker_commands.py`.
