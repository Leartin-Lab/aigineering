# G0b: native direct DeepSeek Chat Completions adapter

Status: isolated seven-file implementation independently Passed. Root full/installed/build and separate independent overall review Passed; final metadata/source-package light closure is tracked in report113. The original Ready plan snapshot is `f623924dac3ab31446f6a8cf08fa13fce86bda29b32be2d1fcb242e87b8f01bc`; this published plan adds delivery metadata. No real provider execution or fair-pair result is claimed.

## Basis and intended boundary

G0b builds on the accepted F1b direct tool loop and the accepted G0a in-process request meter. It adds one versioned native Chat Completions response path for the direct loop while preserving F1b's `offline-v1` wire exactly: scripted responses still require `{message, usage}` with `usage is null`, and the existing `direct-run.py` remains deterministic/offline and never reads environment variables. Live selection is a separate, plainly named `direct-live.py` entrypoint. The adapter uses the frozen ToolRegistry, tool schemas, eval inputs, brief, and external oracle. It bypasses Runtime scheduling and commitment as F1b requires; it does not create Candidates, tasks, claims, attestations, or a second scheduler.

G0b meters admitted request permits and records transport-invocation/response observations plus usage uncertainty for one direct run. A permit is not proof that transport was entered, and transport entry is not proof that the provider received or processed a request. It does not connect `LLMWorker`, `LLMConfig`, configured Runtime/Fleet, Redis, Candidate metadata, or Runtime task logs. G0c must separately plan and implement a Runtime transport connection. Until both arms have an audited common run budget, no result from G0b may be labelled a same-budget comparison. Runtime causal allowance remains work authority, not requests, tokens, or cost.

Planning basis: workflow adapter `3d33000a69fe720c181d9902bc205b0d50692c93ae35a8dd85a5dd5264dd81ce`; the main stage plan was read at historical commit `b9a87cd29143a5871a331a1eb107333e276c96d7` (the current plan file has since acquired unrelated progress metadata and is not a G0b contract input); G0a plan `8b1ecc6e0544186196da99fadbd5d467f5e90e21899a5cccfd37cd6eee6d529f`; F1b plan `5d78743310be35c2b7795b425bf0a653ff1eeab2180da81f65c889f656241ffd`; protocol inventory `0299194b320ea27316a5ce788a1169c9726e1beda34f79ede7be094cc04ea50c`. Direct loop `direct_harness.py` is `ef987c7a71feb000e02183a49921aa08b74f8f3485f79e104ec06111958e8600`; offline CLI `direct-run.py` is `1fb5b6c4a5c6e32b18093d42f4fa5aa47c4542cd9d3b1bca4779130306201653`; G0a module `provider_meter.py` is `1e4139b83ad7a2774b746565bc510dbdb0120eecefae1a43837c107fd3a863f0`.

## Fixed file ownership proposal

G0b owns only these files:

- `examples/thermal_calibration/direct_harness.py`: add a named opt-in `deepseek-chat-v1` response protocol. The current default remains `offline-v1` and keeps all current exact-key, one-tool-call, strict-JSON and `usage is null` checks unchanged. Define a frozen `DirectProviderResponse` with the validated assistant message and finish reason; usage is deliberately not placed in this object or in the offline envelope.
- `examples/thermal_calibration/deepseek_client.py`: synchronous standard-library HTTPS transport, injectable transport/sleeper/monotonic clock for tests, explicit response parser, bounded retry loop, G0a `RunMeter` integration, and safe per-permit/transport-invocation event values. No SDK or dependency addition.
- `examples/thermal_calibration/direct-live.py`: separate live CLI; resolves the API key only after entering this command, reads it from a caller-selected environment-variable name, and never accepts the key as an argument. Writes unique, non-overwritable run evidence.
- `examples/thermal_calibration/direct-README.md`: distinguish the offline fixture from the explicitly live command and document the scope limits.
- `tests/test_thermal_calibration_deepseek_client.py`: fake-transport protocol, attempt, usage and error tests.
- `tests/test_thermal_calibration_direct_live.py`: CLI configuration, safe evidence and no-network-before-valid-live-configuration tests.
- `tests/test_thermal_calibration_direct_harness.py`: regression tests that offline-v1 stays strict and provider-v1 continuation retains its explicitly allowed fields.

No edits to `direct-run.py`, F1a `run.toml`/`workers.toml`/CLI, A tools/oracle/fixtures, G0a meter or its tests, `LLMConfig`/`LLMWorker`, Runtime, Store, Redis, pyproject dependencies, or task protocol. `examples/` is absent from the wheel; a future built sdist must contain the direct package and the separately verified wheel must not be described as containing it. If a necessary implementation needs any file outside this list, return to plan review before editing.

## API and request contract

```python
@dataclass(frozen=True, slots=True)
class DeepSeekConfig:
    base_url: str
    model: str
    thinking_mode: Literal["enabled", "disabled"]

@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    id: str
    name: str
    arguments: str

@dataclass(frozen=True, slots=True)
class ProviderAssistantMessage:
    role: Literal["assistant"]
    content: str | None
    tool_calls: tuple[ProviderToolCall, ...]
    reasoning_content: str | None

@dataclass(frozen=True, slots=True)
class DirectProviderResponse:
    message: ProviderAssistantMessage
    finish_reason: Literal[
        "stop", "tool_calls", "length", "content_filter",
        "insufficient_system_resource", "aborted"
    ]

@dataclass(frozen=True, slots=True)
class TransportReply:
    status: int
    body: bytes                 # bounded to max_response_bytes + 1

class SafeProviderError(RuntimeError):
    __slots__ = ("_code", "_phase", "_status_code", "_retryable")
    def __init__(self, *, code: str, phase: str,
                 status_code: int | None = None,
                 retryable: bool | None = None) -> None: ...
    code: Literal[
        "configuration", "request_budget_exhausted", "deadline_exceeded",
        "transport_timeout", "transport_error", "http_status",
        "response_too_large", "invalid_json", "invalid_response",
        "unsupported_finish_reason", "retry_exhausted", "internal_error"
    ]
    phase: Literal["configure", "permit", "dispatch", "read", "decode", "validate"]
    status_code: int | None
    retryable: bool | None

@dataclass(frozen=True, slots=True)
class DirectLiveLimits:
    max_attempts: int  # admitted permits; never label this confirmed provider requests
    max_output_tokens_per_request: int
    output_reservation_ceiling: int
    deadline_seconds: float
    request_timeout_seconds: float
    max_model_calls: int
    max_tool_attempts: int
    max_response_bytes: int
    max_arguments_bytes: int
    max_final_bytes: int
    max_http_retries: int
    retry_backoff_seconds: float

@dataclass(frozen=True, slots=True)
class DirectLiveConfig:
    case_id: str
    output_dir: str
    run_id: str
    api_key_env: str
    provider: DeepSeekConfig
    limits: DirectLiveLimits

@dataclass(frozen=True, slots=True)
class ProviderAttemptEvent:
    ordinal: int  # admitted permit ordinal; unique within run
    event_type: Literal["permit_admitted", "transport_entered", "response_observed", "permit_finalized"]
    outcome: Literal["response", "failed", "timeout", "pending"] | None
    status_code: int | None
    error_code: str | None
    error_type: str | None
    retryable: bool | None
    duration_seconds: float | None
    finish_reason: str | None
    late: bool | None

@dataclass(frozen=True, slots=True)
class DirectLiveResult:
    report: Mapping[str, object]
    oracle_result: Mapping[str, object]
    usage: RunUsageSummary
    event_count: int

class Transport(Protocol):
    def __call__(self, *, url: str, headers: Mapping[str, str], body: bytes,
                 timeout_seconds: float, max_response_bytes: int) -> TransportReply: ...

class DeepSeekChatCompletionClient:
    def __init__(self, config: DeepSeekConfig, *, secret: str,
                 limits: DirectLiveLimits, meter: RunMeter,
                 transport: Transport | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep) -> None: ...
    def complete(self, *, messages: list[dict[str, Any]],
                 tools: list[dict[str, object]], timeout_seconds: float
                 ) -> str | DirectProviderResponse: ...
    def __repr__(self) -> str: ...  # static safe repr; secret always redacted
```

This signature matches the actual keyword-only `ModelClient.complete` call in `direct_harness.py`: the loop supplies exactly `messages`, `tools`, and `timeout_seconds`. Offline fixture clients return JSON text and go through unchanged `offline-v1`; only the explicitly selected live client may return `DirectProviderResponse`. The live loop branch converts its validated message into the in-memory assistant shape, including the separately allowed `reasoning_content`; it does not serialize that shape through the offline envelope. Per-request `max_tokens` comes from the meter-backed client config/permit, not a new loop argument. No `n` request parameter is sent because the official request schema does not document it. The non-streaming response invariant requires one local `choices` item at index 0. Both loop and meter receive the same injected monotonic callable; the meter is constructed immediately before `run_direct`, actual timeout is the minimum of loop remaining and permit timeout, and the meter deadline is the stricter eligibility fence if their start timestamps have a small skew.

`DirectLiveLimits` validation is exact and happens before reading the key. Integer fields require `type(value) is int` (bool rejected): `max_attempts >= 0` and `output_reservation_ceiling >= 0` (zero disables all provider requests), `max_output_tokens_per_request` in `1..393216`, `max_model_calls > 0`, `max_tool_attempts > 0`, and byte caps `max_response_bytes`, `max_arguments_bytes`, `max_final_bytes > 0`. `request_timeout_seconds` and `deadline_seconds` must be finite positive numbers excluding bool. `max_http_retries` is an integer >=0 (0 means only the initial attempt); `retry_backoff_seconds` is finite >=0 excluding bool. Also validate `max_output_tokens_per_request <= output_reservation_ceiling` whenever the ceiling is nonzero, preventing an unusable first request. Unknown fields, NaN/infinity, negative values, bool-as-int, floats for integer fields and string coercions are rejected, never silently converted. Every directly constructed config repeats these checks rather than trusting Click parsing. For this stage, `model` must be the explicitly selected documented `deepseek-flash` value; a new model identifier needs plan review.

`DeepSeekConfig` contains only provider endpoint/model/thinking selection; `DirectLiveLimits` is the sole source of caps, timeouts, and retry values. Both and every response value are frozen. `DirectLiveConfig` contains no key value, only the environment-variable name. Validate the name against `[A-Za-z_][A-Za-z0-9_]*` with all other non-secret configuration before creating output. After the run directory and safe evidence sink are ready, resolve the value and pass the secret straight to the client constructor. The client retains only a private Authorization-header value needed for subsequent calls, excluded from repr/equality; its explicit repr is static and says `secret=<redacted>`. It does not retain a separate printable raw-key field. A missing/empty environment value yields safe `configuration` failure without HTTP. `ProviderAttemptEvent`, `DirectProviderResponse`, `TransportReply`, and `DirectLiveResult` are frozen snapshots. Emit lifecycle events as separate append-only records; event `event_type` distinguishes admission, invocation, full response, and meter finalization, while `ordinal` identifies the permit. Counts are computed from those recorded event types and are not inferred from permit count; nested report/result mappings are copied to immutable mappings before return. `DirectLiveResult` is returned only after a final report passes the oracle and contains `report`, `oracle_result`, `usage`, and `event_count`. Failures produce safe typed errors and mapped CLI exit codes. Evidence exists only after successful exclusive run-directory creation and sink/initial-manifest setup; earlier input/path failures have safe stderr codes only, not run artifacts. The validated `report` may be written to `report.json`; stdout remains summary-only. Tests assert that `repr(config/client/error)` and safe output never contain a sentinel key, URL userinfo/query, response body, prompt or reasoning marker.

`SafeProviderError` constructor is keyword-only: `SafeProviderError(*, code, phase, status_code=None, retryable=None)`. It stores no request, response, URL, header, secret or original exception text. Map all expected failure paths as follows. Invalid non-secret configuration/path syntax and output collisions occur before key resolution and have no permit; missing-key evidence is required only after a valid new run directory and initial sink/manifest are successfully created.

| Condition | Meter finish | Retry | Safe error/result |
|---|---|---|---|
| Invalid non-secret configuration/limits/path syntax/env-name | No run directory, key lookup, or transport; safe stderr code only | No | `configuration` / exit 2 |
| Output path collision/filesystem creation failure | No key lookup/transport; do not touch existing path | No | safe `configuration` / exit 2 |
| Event sink or initial manifest open/write failure after exclusive mkdir | Best-effort safe stderr only; preserve partial new directory; no key lookup/transport | No | `internal_error` / exit 70; no durable-evidence claim |
| Missing/empty selected key | Safe failure event/final manifest in new directory if sink writable; no transport | No | `configuration` / exit 2 (sink failure becomes exit 70) |
| Meter rejects attempt permit | No permit | No | `request_budget_exhausted` or `deadline_exceeded` / exit 3 |
| Socket timeout after permit | `timeout`, pass `None` to G0a (`not_returned`; no root response object) | No | `transport_timeout` / exit 4 |
| Network error after permit | `failed`, pass `None` to G0a (`not_returned`; no root response object) | No | `transport_error` / exit 4 |
| Fully received bounded HTTP 500/503 body | `failed`; for a decoded root JSON object, pass its top-level `usage` member unchanged to G0a (`None` if absent/null); if no complete root object was decoded, pass `None` | Yes, even if complete bounded body JSON is malformed; only while retries remain and deadline/backoff/next permit allow | `http_status` per failed attempt; after last, `retry_exhausted` / exit 5 |
| Oversized 500/503 or body-read failure | `failed`, pass `None` to G0a (`not_returned`; no complete root object) | No: body was not fully received within cap | safe `response_too_large`/`transport_error` / exit 5 or 4 |
| HTTP 429 or any other non-2xx | `failed`; for a decoded root JSON object, pass its top-level `usage` member unchanged to G0a (`None` if absent/null); if no complete root object was decoded, pass `None` | No | `http_status` / exit 5 |
| 2xx with invalid/oversize JSON | `response`; pass top-level `usage` unchanged only if a complete root JSON object was decoded; otherwise pass `None` | No | `response_too_large` or `invalid_json` / exit 6 |
| 2xx decoded root object but invalid choice/message/reason | `response`; pass top-level `usage` unchanged (`None` if absent/null) | No | `invalid_response` or `unsupported_finish_reason` / exit 6 |
| Response is valid but meter says late | `response`, preserve usage, `late=true` | No | `deadline_exceeded` / exit 3; do not return turn |
| Event sink/clock/unexpected error after permit admission | Leave pending if `finish` has not atomically completed | No | Propagate the original exception to the CLI boundary; CLI writes only `internal_error`, safe type and exit 70; no retry |

If JSON is malformed or exceeds the cap, do not try to salvage tokens from a partial body. A malformed but fully received body on status 500/503 remains retryable and finalizes with `usage=None` (`not_returned`); an oversized/body-read-failed response is not fully received and is terminal. For any status, usage extraction follows one rule: only after decoding a complete bounded root JSON object, read its top-level `usage` member and pass that raw JSON value unchanged to `RunMeter.finish`; absent or null usage passes `None`. Object mappings preserve each valid/missing/invalid component independently; non-mapping usage values pass unchanged and G0a reports its core components as `invalid`. If no complete root object is decoded (including malformed JSON, oversized body, read failure, or a valid top-level JSON array/scalar), pass `None`; never infer or salvage a usage member from partial bytes or invent a value. A retry is not a new run: the same run ID and meter remain active, each attempt has a unique ordinal and permit, all earlier events remain recorded, and no prior evidence is overwritten. An output directory is atomically created exclusively before key lookup or HTTP; if it exists, fail rather than resuming or deleting it. A caller may choose a new run ID for a new run, which creates a fresh meter and cannot claim to resume or qualify the previous run.

All config/result dataclasses are frozen. `SafeProviderError` has only the four slotted private fields shown above and read-only properties `code`, `phase`, `status_code`, `retryable`; initialize `Exception.args` with only the safe code. The API key is a keyword-only secret parameter to the client constructor, never a config/result/error field or repr value. The client retains only a private Authorization-header value needed for repeated requests; its explicit `repr` is static and redacts this value. Safe errors expose only `code`, `phase`, optional integer `status_code`, and optional `retryable`; public events record only those plus safe exception type, never `str(exc)`, response text, or chained transport exceptions. Catch only expected transport exceptions and raise safe errors `from None`; unexpected failures propagate unchanged to the CLI boundary, which records only safe type/code and never retries them. No public traceback or private exception message is emitted.

### HTTP and response shape contract

The configured base URL must use `https://api.deepseek.com`, with no explicit port, userinfo, query or fragment; its path prefix is either empty or `/v1`. Append `/chat/completions` to that prefix. Reject controls/path traversal. Set `stream: false` explicitly. Send only request keys `model`, `messages`, `tools`, `max_tokens`, `thinking: {"type": configured_mode}`, and `stream: false`; do not send undocumented `n`. A redirect handler refuses all redirects so Authorization cannot be forwarded to another origin. Production transport reads at most `max_response_bytes + 1` bytes and rejects overflow before decoding; it must never read an unbounded body and check afterward. The injected transport must obey the same bounded-read contract, and the client independently rejects a reply whose body exceeds the cap. `TransportReply.status` must be `type(status) is int` in `100..599`; `body` must be bytes. No response headers are retained or exposed. Never include HTTP error bodies in exceptions.

Accept only one bounded non-streaming JSON response object. Required top-level keys: `id`, `object`, `created`, `model`, `choices`; `system_fingerprint` and `usage` are optional or null. `object` must be `chat.completion`; id/model are nonempty strings; if `system_fingerprint` is present and non-null it is a string (empty is allowed); created is a nonnegative integer excluding bool. This explicitly accepts the documented nullable/omitted fingerprint form. Usage observation is separate from response-protocol validity. For every bounded, duplicate-free JSON object envelope, independently extract `usage` as an unconstrained JSON value: omitted or null becomes `None`; object values are passed unchanged to `RunMeter.finish` so it preserves absent/partial/invalid core fields componentwise while retaining valid siblings; every non-object non-null value (boolean, array, string, or number) is also passed unchanged and G0a marks each core component `invalid`. Do not coerce it to a mapping, filter it, or add an adapter-only `usage_shape` field. The JSON protocol parser retains the raw usage value only in memory until meter finalization; safe evidence contains the resulting G0a component statuses and aggregates, not the raw usage value. Do not reject a response solely because usage is incomplete/invalid. Optional documented cache/reasoning detail values are ignored by G0a and remain unsupported; do not make claims about them. Unknown top-level response keys remain protocol-invalid, but a complete bounded envelope with such keys still contributes independently extractable usage before the response is rejected.

`choices` must contain exactly one item; its `index`, `finish_reason`, `message`, and `logprobs` are required; index is integer 0 excluding bool and `logprobs` must be null. The message requires role and nullable content, role exactly `assistant`; optional `reasoning_content` is string or null; optional `tool_calls` is an array with at most one item. No other choice/message fields are accepted. Each tool call has exactly id/type/function; type is `function`, id nonempty text, and function has exactly string name and string arguments. Arguments still pass the existing strict JSON and tool-schema validation before execution. Unknown keys or incompatible types yield `invalid_response`; an unknown finish reason yields `unsupported_finish_reason`.

`stop` with no tool calls and nonempty string content is the only final-report path. `tool_calls` requires exactly one call and `content is null`, the only continuation path. With thinking enabled, a tool continuation must include string `reasoning_content` (empty string is valid); retain it only in the in-memory transcript and send it unchanged in the next request that includes tools. Thinking-disabled responses allow absent/null reasoning only. The assistant report is parsed by the existing strict report parser and checked by the external oracle. An oracle-checked report may be written to `report.json`. `length`, `content_filter`, `insufficient_system_resource`, and `aborted` are typed terminal failures; none reaches the oracle as a completed report. Finish/message mismatches fail closed.

The configured `model`, redacted base URL (scheme/host/path only), thinking choice, output cap, run limits and brief/input hashes are written to safe evidence. Defaults must not conceal a live choice: `direct-live.py` requires explicit model, base URL, thinking mode, API-key environment-variable name, request timeout, maximum admitted permits, per-attempt output cap, aggregate output-reservation ceiling, run deadline, model-turn/tool limits, HTTP retry count/backoff, case, output root and run ID or generated unique ID. It validates all limits before reading the key or constructing transport. `direct-run.py` remains offline with its current constants and manifest schema.

## Every-attempt meter and failure semantics

`direct-live.py` constructs `RunBudget(max_attempts=limits.max_attempts, max_output_tokens_per_request=limits.max_output_tokens_per_request, output_reservation_ceiling=limits.output_reservation_ceiling, deadline_seconds=limits.deadline_seconds)` and one `RunMeter` using the exact same injected monotonic callable passed to `run_direct` and the client. Immediately before each planned transport invocation, including each configured retry, the client calls `RunMeter.acquire_request(output_cap=limits.max_output_tokens_per_request, request_timeout_cap=min(limits.request_timeout_seconds, timeout_seconds))`. After permit success, emit a `permit_admitted` event. Immediately before calling the injected/production transport emit `transport_entered`; after a complete bounded reply emit `response_observed`. A failure between admission and transport entry leaves an admitted pending permit and has no transport invocation. Once transport is entered, remote receipt/processing remains unobservable even if no response arrives. Report these distinct counters; never call any of them an exact provider-received-request count. The returned permit timeout is the transport timeout and the permit output cap is the request `max_tokens`; a denied permit produces a safe `request_budget_exhausted` / deadline result and no transport invocation. The client calls `finish` exactly once for each permit unless an unexpected meter/clock/sink failure prevents finalization, in which case the record truthfully remains pending.

- Network failures and timeouts finish the permit as `failed`/`timeout` with `usage=None` (`not_returned`, because no complete root response object exists); default is zero retries. Retry eligibility is decided by fully receiving a body within the cap and observing status 500 or 503; JSON validity is not required, so malformed but complete 500/503 bodies finalize with `usage=None` (`not_returned`) and may retry if retry budget and deadline permit. An oversized response or body-read failure is not fully received within the cap, finalizes with `usage=None`, and never retries. 429 and every other status terminate without retry. Backoff uses `sleep_for = min(configured_backoff, remaining_deadline)` and never asks the sleeper to exceed remaining time. After sleep, recheck monotonic time; if `now >= deadline`, terminate without a new permit. Equality is expired. If the backoff reaches or exceeds remaining time, no retry dispatch occurs. If it fits strictly before deadline, acquire a fresh permit; permit denial also stops without dispatch. Each retry requires a fresh permit and a finalized preceding attempt. Parser/validator errors, meter/clock exceptions, sink errors, unexpected exceptions and uncertain permit states are never retried. Retrying never reuses a reservation.
- For every status, a complete bounded root JSON object with a `usage` member passes that raw member unchanged to `RunMeter.finish`; absent/null passes `None`. This applies equally to 2xx, retryable 500/503, and non-retryable HTTP statuses. Partial usage mappings retain reported sibling components and missing/invalid states; non-mapping values retain G0a invalid states. A malformed/oversized/read-failed response, transport timeout/failure, or valid non-object root JSON has no decoded top-level usage and passes `None`. Usage is finalized independently of status/choice validation. A 2xx response with invalid choice/message finalizes `outcome="response"` with the observed usage state and then raises a safe protocol error; it records receipt of a response without making that model turn acceptable. Clock/sink/unknown failure before `finish` commits means no retry and the meter snapshot truthfully leaves the permit pending.
- For absent/null usage, G0a records `not_returned`; missing, invalid, bool/negative fields keep its exact component states. Valid provider usage goes only to `RunMeter.finish`, never the F1b offline wire or Candidate metadata.
- A malformed choice or disallowed finish reason retains the decoded raw usage member and its G0a component states, and fails the direct run. Unknown usage is never zero. A response late under either meter or loop deadline is retained as an observation but cannot reach a tool or oracle.
- For a received 2xx, meter `result_eligible` only means `outcome="response"` and completion strictly before its cooperative deadline. It does not mean valid JSON, an allowed finish reason, a valid tool call, an oracle-passing report, or accepted business output. The direct loop separately validates each of those before returning a result.

Use G0a's exact output reservation semantics: the entire per-attempt `max_tokens` is reserved before dispatch and is never refunded, including failed/unknown attempts. It is a capacity reservation, not actual usage. G0a reports actual prompt/completion/total components but does not impose an aggregate actual-token ceiling, tokenize prompts before dispatch, or calculate price. Therefore G0b can truthfully enforce admitted permits (which upper-bound transport invocations), per-request output cap, total reserved output capacity, and cooperative deadline; it cannot claim a hard aggregate input-token cap, aggregate consumed-token cap, cost cap, or same-budget parity with Runtime. Do not relabel Runtime allowance as tokens.

The meter and direct loop use the exact same injected monotonic callable. Construct `RunMeter` immediately before `run_direct`; request timeout is capped by loop `timeout_seconds`, then by permit timeout. Synchronous calls are cooperative, not forcibly interrupted. A response completing at or after either effective deadline is retained as usage but rejected as a model result; meter equality (`completed >= deadline`) is late. Meter is the stricter eligibility fence if construction causes small start skew. Record relative start skew/deadline values, not absolute clock values. No later tool execution or report qualification occurs after deadline detection. Keep oracle result separate from request-budget qualification: unknown usage prevents asserting complete usage totals; `result_eligible` does not imply usage complete or business acceptance.

## Output and secret handling

The live manifest is a separate versioned schema and records selected case/standard/brief/tool/oracle hashes, source hashes, model/redacted base URL/thinking settings, explicit limits, attempt ordinal, status code, safe error code/type, duration, finish reason, late state, G0a usage statuses/subtotals/unknown counts, oracle status, run phase and exit code. It never records the API key/value, Authorization headers, prompts/request body, raw provider response/reasoning content, tool arguments, raw provider errors or full transcript. The validated business report may be written to `report.json`; stdout prints only a safe summary and output path. Events JSONL is allowlisted to these safe values and flushed after each event. A run directory is created exclusively; existing output paths fail before key read/network. After the new run directory and initial sink exist, failures retain best-effort safe manifest/event/error code and are never overwritten by a rerun; prior validation/path failures create no run artifacts. Do not print raw response payloads; this does not prohibit returning/writing the final oracle-checked business report.

The CLI validates all non-secret limits/configuration before creating output or resolving the named environment variable; a missing key is detected after the new directory and initial evidence sink are ready, but before opening transport. The API key is read only in `direct-live.py`, after full non-secret validation, exclusive directory creation, and successful initial safe evidence sink/manifest setup; it is passed directly to the client without logging. Offline tests monkeypatch a dummy key and fake transport; no API call is made by tests or by this drafting work.

## Offline verification and dependencies

Use only fake HTTP responses and fake clock/sleeper; no provider, HTTP, Redis, Store or Runtime fixture. Assert:

1. Golden text final, exact report parsing and same Fraction oracle; one choice only. Request assertion proves there is no `n`, `stream` is false, `max_tokens` equals the meter permit cap, and only the frozen request keys are sent.
2. Tool continuation sends the original assistant `reasoning_content` and `tool_calls` back on the next request, then pairs exact tool-call ID/result. Thinking disabled handles absent/null reasoning. Offline-v1 still rejects any added key or non-null usage.
3. `finish_reason=length`, `content_filter`, `insufficient_system_resource`, `aborted`, unknown reason, malformed/duplicate-key JSON, missing/multiple choices, malformed usage/message/tool call and call-count mismatch stop before oracle/handler as appropriate. If a complete root object exists, raw usage is still passed to G0a; otherwise `None` is passed.
4. Valid, null, missing, partial and invalid usage preserve G0a component state for 2xx, retryable 500/503, and non-retryable HTTP status. Cover raw usage values that are objects, null, booleans, arrays, strings, and numbers; also cover absent usage and no complete root JSON object. Known nonzero components on failed HTTP or invalid 2xx choice remain in the meter; no `cached_tokens`/`reasoning_tokens` support is claimed.
5. 500/503 retry eligibility depends on complete bounded body receipt, not valid JSON; malformed but complete bodies pass `None` to G0a (`not_returned`) and may retry, while oversized/body-read-failed bodies are terminal and also pass `None`. A fresh permit is required for every retry transport invocation. Fake clock/sleeper cases cover backoff strictly fitting, reaching, and exceeding remaining deadline, plus a retry permit denied by attempt or output reservation. At/after deadline no retry dispatch occurs; 429 and other statuses stop.
6. Timeout, malformed body, oversized body, retry exhaustion and late valid response retain safe failure evidence; no answer reaches tool/oracle after lateness; secret/prompt/reasoning/raw-response markers are absent from stdout/stderr/events/manifest. A local test confirms the production body reader stops at cap+1 and redirect responses never forward Authorization. Parameterize every limit over bool, negative, NaN, infinity, wrong numeric type, zero where disallowed, and exact allowed boundary; verify invalid limit and invalid env-name make no directory/key lookup/transport; collision does not touch the existing path and makes no key lookup/transport; missing/empty key writes safe failure evidence and makes zero transport calls; pre-request sink failure makes zero key lookup/transport and returns safe `internal_error`/exit 70 without claiming durable evidence.
7. Inject pre-request events/initial-manifest sink failure: no key lookup or transport, safe stderr `internal_error`/exit 70, no durable-evidence claim. Inject post-permit clock/event-sink failure and an unexpected transport exception: no retry, at most the already-entered transport invocation, and a pending permit if finalization did not commit. Verify the original unknown exception is not relabelled as a retryable network failure.
8. `direct-live.py` uses fake transport under explicit selected settings, writes one immutable run directory, and leaves direct-run.py offline behavior and evidence byte schema unchanged. Architecture test forbids imports/calls of Runtime/Fleet/Store/Redis/LLMWorker/HarnessCandidateAdapter and forbids new project dependencies.

Run targeted tests and Ruff in the exact source snapshot. Build sdist/wheel only if applicable to the eventual package delivery; validate that examples bundle is from sdist and not wheel. Record every command, exit code, module/file hashes and retained first failure. No real provider test is part of this package plan.

## Bounded source contract

The source hashes above identify the exact F1b/G0a interfaces inspected for this plan. Before implementation, freeze the actual current source files the implementer uses and compare their required signatures/semantics with this contract. Changes to those interfaces or to G0b-owned behavior require plan re-review; unrelated stage-plan status/progress metadata and changing aggregate document hashes do not reset this contract. The old main-plan hash noted in the predecessor draft was stale; this revision binds the already-read historical baseline by commit and does not pretend to hash mutable progress text.

## Dependencies, review, and later stages

Plan dependencies: F1b's existing direct-loop exact contract and G0a's meter API; both are independently accepted. The runtime implementation of `RunMeter.finish` accepts an unconstrained usage value and classifies non-mappings as three invalid components; despite its narrower Mapping annotation, the adapter passes the decoded JSON value unchanged (no cast/coercion/filter) and pins this observed behavior to the frozen G0a source. Before implementation, a non-author audits this file, the listed interface source hashes and the explicit compatibility boundary. Author owns only listed files; root integrates; `integration_review` independently accepts the exact implementation snapshot. Any change to F1a model/thinking configuration, offline wire, provider usage schema, Runtime/LLM transport, retry owner, dependencies or evidence authority returns to plan review.

G0b is not a prerequisite for continued offline CLI/business debugging. G0c is a separate Runtime transport/meter connection package and cannot import G0b's direct client as a hidden second scheduler. A later G pair plan must freeze both arms' model revision/parameters, thinking policy, admitted-permit cap and separately observed transport invocations, per-request output cap, output reservation, deadline, unknown-usage rule, retry schedule, run order, failure schedule, inputs/tools/oracle and human work accounting. If equal aggregate token limits cannot be enforced in both arms, call it separate-budget research rather than same-budget comparison. For an actual provider run, use the already-confirmed current session authorization only within its stated scope; seek fresh user direction only if the proposed spend or action exceeds that scope.

## Official API field check

Checked 2026-10-24 against the provider's official docs. Chat Completions currently lists `deepseek-flash`, one completion response with `choices[].message`, `finish_reason`, and top-level `usage`; usage has prompt/completion/total counts plus cached/reasoning subfields. Thinking is enabled by default unless explicitly disabled. In tool-enabled thinking requests, the docs require preserving prior assistant `reasoning_content` on every subsequent request carrying `tools`. The docs list finish reasons and recommend retry after 500/503, while rate-limit 429 guidance says pace requests. Recheck these fields before any later live run; endpoint behavior/model availability can change.

- https://api-docs.deepseek.com/api/create-chat-completion/
- https://api-docs.deepseek.com/guides/tool_calls/
- https://api-docs.deepseek.com/guides/thinking_mode/
- https://api-docs.deepseek.com/quick_start/error_codes/


## Implementation and review record

Luna `provider_plan_v2` authored the revised plan and seven implementation files. The Ready plan received non-author audit (`e5c22a40e91886635ddcf1371f91bbc6c39a15fddae851f05e10d74d049548db`). Non-author `integration_review_v2` reconstructed candidate4 exactly and Passed it: seven-file map `77436390cac09f79c1b0d39fd0cdd7eb6c7e52b57df7bb5e0c7eeef2fc30d228`, review `a6bca189e4a9d2e6c5ef5d5280660d40e2d35b5f3b83488c4d4e90a1cce724ba`. Independent checks ran 209 owned tests, ten boundary probes and one report-write I/O probe; no real network or key was used.

Earlier candidates failed independent tests for untrusted tool-call IDs in live logs, premature fixed/chunked response EOF handling, cap+1 stops incorrectly treated as EOF, and non-2xx oversize CLI exit classification. Candidate4 closes these exact findings; original reds remain. Exact tool IDs/reasoning are preserved only for in-memory continuation; log fields omit untrusted IDs. Partial bodies cannot supply usage. Root-owned combination evidence belongs in the [development report](../../reports/113-asset-context-direct-transport-development-2026-10-25.md). Runtime metering, controlled real-provider protocol and business/cost conclusions remain separate open work.

Root预声明四切片及三文件hook维护组合：源码3,532项与树外656项、Ruff/格式、构建及安装驱动检查通过。717文件代码/夹具摘要e87654c18b07100d555247e2e15989ffaa28c598e8e602d43158f1c2ee9da9ca；非作者对准确组合独立总体验收Passed；最终验收元数据/源码包按同一717文件代码快照轻闭合，详见report113。以上不是本组件作者单独完成的全套，也不替代本轮未做的真实provider/整体规模资格。
