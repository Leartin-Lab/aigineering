# Direct Harness: offline and opt-in live modes

The existing `direct-run.py` is an offline scripted fixture. It does not read provider configuration or credentials and keeps the `offline-v1` response envelope. Run it from a source checkout as documented in the example README.

`direct-live.py` is a separate, explicit live command. It requires all model, endpoint, thinking, timeout, attempt, output-cap, reservation, retry, case, and output settings, plus the **name** of an environment variable containing the key. The key itself is never a command-line argument. Example shape (this command makes a real provider call if the named environment variable is set):

```sh
python examples/thermal_calibration/direct-live.py \
  --case short-interval \
  --output-root /tmp/calibration-live-runs \
  --base-url https://api.deepseek.com/v1 \
  --model deepseek-flash \
  --thinking-mode disabled \
  --api-key-env DEEPSEEK_API_KEY \
  --request-timeout 20 --max-attempts 8 \
  --max-output-tokens-per-request 2048 \
  --output-reservation-ceiling 8192 --deadline 180 \
  --max-model-calls 8 --max-tool-attempts 6 \
  --max-response-bytes 1048576 --max-arguments-bytes 524288 \
  --max-final-bytes 262144 --max-http-retries 1 --retry-backoff 1
```

Use a fresh run ID or allow the command to generate one. The output root must already exist. Each run creates an exclusive directory; a collision is an error and does not resume or alter the prior run. Configuration is validated before directory creation or key lookup. After creating a new run directory, the command opens the append-only event log and initial manifest before resolving the named environment variable. If those evidence files cannot be initialized, the command exits without reading the key or calling the provider. Missing or empty key values are recorded as safe configuration failures when the initialized sink remains writable.

The live client uses bounded standard-library HTTPS, refuses redirects, and records admitted permits separately from transport invocations and observed responses. None proves that the provider received or processed a request. A permit reserves its full output-token cap even when the call fails. Provider token usage is recorded only when returned in a complete bounded JSON response; missing or malformed usage remains unknown, never zero. This adapter does not enforce an aggregate input-token or consumed-token budget, calculate provider prices, or connect the Runtime transport. Its results are not a same-budget Runtime comparison.

For native DeepSeek tool-call responses, the adapter requires the call `id`, `type`, and `function`, and requires the function `name` and JSON `arguments` strings. It discards additional metadata keys on those two objects without inspecting their values. A tool-call response with `content` equal to `null` or the empty string maps to the harness's canonical `content=None`; other content values are rejected. The adapter preserves the tool-call ID, function name, arguments, and enabled reasoning needed for continuation. This native response mapping does not change the strict `offline-v1` fixture envelope or the harness's canonical DTO.

Evidence includes safe configuration, hashes, response status/phase, request-meter usage states, and the validated business report. It omits the key, headers, prompts, raw response, tool arguments, and reasoning text. A final report is written only after the frozen external oracle accepts it. Offline fixtures and this public repository's eval data are not confidential holdouts. The examples package is included in the source distribution, not the wheel.

Tests use fake transports and dummy environment values. They do not call a provider or open network connections. A live run is a separate operator action and requires its own bounded authorization.
