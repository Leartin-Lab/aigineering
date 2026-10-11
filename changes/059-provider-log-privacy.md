# Provider error log privacy

Non-retryable provider failures now log the classified status code without formatting the provider exception body. Retry and retry-exhaustion logs continue to expose their category, attempt count, and status code. The original `ProviderError` and its message are unchanged for callers, so this bounds operational log exposure without changing runtime error semantics.

Regression coverage injects local fabricated 401 and 503 failures through the worker transport; no provider request is made. This does not sanitize other loggers or guarantee that arbitrary application logs never contain sensitive values.
