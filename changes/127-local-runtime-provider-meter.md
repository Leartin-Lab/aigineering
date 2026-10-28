# Change 127: Meter provider attempts in an ordinary Runtime profile

## Result

An opt-in source-checkout profile connects a shared process-local request meter
to the Planner and Researcher `LLMWorker` transports. Both reserve a permit
before transport and share attempt/output-reservation/deadline ceilings, with
zero Worker retries. A new process starts a new epoch; budgets are not restored
after restart.

Four typed auxiliary events describe permit admission, transport entry,
response observation and finalization. Exact closed schemas exclude credentials,
raw usage mappings, bodies, prompts and arbitrary exception text. Admission or
transport entry does not prove provider receipt. Unknown usage remains unknown;
these observations are not signed facts or acceptance evidence.

The configured profile's request/output/read limits are explicit configuration,
not general provider guarantees. Existing Worker compilation, signed claims,
tool continuation, verification, commitment and recovery owners remain in use.
The ordinary example profile is unchanged. No scheduler, persistent budget,
billing policy or extra provider retry is introduced.

## Limits

Fixture transport checks are not real-provider execution. A process-local meter
does not establish persistent cross-process budgets, token billing, provider
quality or successful recovery. Runtime progress and output acceptance still
require their own durable facts.
