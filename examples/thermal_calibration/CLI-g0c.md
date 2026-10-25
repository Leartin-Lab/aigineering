# Ordinary Runtime run with shared provider request limits

This is a source-checkout profile: `examples/` is not included in the installed
wheel because the registry loader imports `examples.thermal_calibration.tools`.
The ordinary `aig run` command and local Worker registration path require the
repository root on `PYTHONPATH` so the selected tool registry can be imported.
Planner and Researcher are local factories that return the existing
`LLMWorker`; the tool Worker and verifier retain their normal registrations.
Runtime still owns task planning, tool continuation, invocation failure,
recovery, claims, output commitment, and independent acceptance.

Set `AIGINEERING_REDIS_URL` to a reachable Redis deployment before invoking
the configured run. Redis is required application infrastructure for Runtime
task discovery, Worker wakeups and event relay; it is not optional for this
CLI profile. The URL below is only an example and should be replaced with the
Redis endpoint and database assigned to the selected workspace.

The two provider Workers share one process-local request meter. Set these
bounded inputs before launching:

```sh
cd /path/to/aigineering
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export AIGINEERING_REDIS_URL='redis://127.0.0.1:6379/0'
export AIGINEERING_G0C_MAX_ATTEMPTS=8
export AIGINEERING_G0C_OUTPUT_RESERVATION_CEILING=65536
export AIGINEERING_G0C_RUN_DEADLINE_SECONDS=900
export AIGINEERING_API_KEY='...'
aig -C /path/to/workspace run \
  --config "$PWD/examples/thermal_calibration/run-g0c.toml"
```

The selected credential is only `AIGINEERING_API_KEY`. The Runtime preset does
not fall back to `OPENAI_API_KEY`, and the selected value is passed directly to
the two ordinary LLMWorkers. The meter is shared only inside this process and
is never restored or reset from a prior run. Output reservations are request
limits, not billed token usage or task allowance.

The run writes its existing auxiliary Worker log. The four additional event
types record permit admission, entry to the bounded transport, complete
response metadata, and successful G0a finalization. They contain no request or
response body, prompt, tool arguments, reasoning, credential, raw usage object,
or arbitrary exception text. Capture is auxiliary: a capture warning does not
change Runtime execution. A missing/invalid credential or invalid budget
configuration fails local Worker preparation before any provider transport.

For an offline fixture, replace the two provider factories in a private test
configuration with test Workers; do not claim that a fixture run is provider
evidence. A successful task must still complete ordinary verification and
independent acceptance. A scheduled recovery alone is not completion.
