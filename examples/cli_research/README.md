# CLI research through ordinary runtime tasks

This example analyzes a small synthetic temperature sample using a model-planned
task tree, two deterministic tools and a different authorized verifier identity.
It publishes a Skill, the input, one root Contract and Worker configuration.
There is no example scheduler or predeclared business task graph. The tools
compute the sample mean and convert that result to Fahrenheit; the verifier
recalculates from the frozen source before qualifying the exact report.

Use an empty workspace and an installed Aigineering package. The checkout must
also be importable for the explicitly selected example tool adapter:

```bash
export AIGINEERING_CHECKOUT=/path/to/aigineering
export PYTHONPATH="$AIGINEERING_CHECKOUT${PYTHONPATH:+:$PYTHONPATH}"
# For source execution, also put "$AIGINEERING_CHECKOUT/src" on PYTHONPATH.
export AIGINEERING_API_KEY=your-test-key
export AIGINEERING_REDIS_URL=redis://127.0.0.1:6379/0
mkdir -p /path/to/empty-workspace
aig -C /path/to/empty-workspace run \
  --config "$AIGINEERING_CHECKOUT/examples/cli_research/run.toml" \
  --wait-timeout 300 --json
```

Redis must be running. The checked-in profiles use `deepseek-flash`. The planner
uses strict JSON actions without native tools or thinking; the researcher uses
native scoped tools with thinking, and the verifier uses thinking with strict
JSON actions. Routing separates planning, business production and verification.
Edit the
Worker configuration explicitly to use another compatible
provider. Credentials are environment variables and never task input Assets.
The root's 32 units bound causal work; they are not a token quota or billing
estimate. Provider usage can remain unknown.

Use the returned exact `contract_id` for inspection, continuation and export:

```bash
aig -C /path/to/empty-workspace task status TASK_ID --json
aig -C /path/to/empty-workspace task audit TASK_ID --json
aig -C /path/to/empty-workspace run \
  --config "$AIGINEERING_CHECKOUT/examples/cli_research/run.toml" \
  --resume TASK_ID --wait-timeout 300 --json
aig -C /path/to/empty-workspace task result TASK_ID \
  --output measurement_report --to /path/to/report.json --json
```

Incomplete runs and unqualified exports return nonzero. Inspect the projected
blockers and failure details; never reset a claim or treat a scheduled recovery
as executed work. Configured Fleet slots continue pulling ordinary eligible
tasks after a recorded invocation failure. A failed root remains terminal: if
its recovery is a separate root successor, explicitly select that recovery ID.
`--once` runs at most one claimed package and can stop before any tools run.
Resume uses the committed Contract and inputs; it does not reread sample files.
Export refuses existing destination files.

For a controlled failure experiment only, set
`AIG_MEASUREMENT_FAIL_ONCE_FILE` to a nonexistent file in a private existing
directory before launch. The first `summarize_sample` returns a failure envelope.
Its explicit output schema requires `success=true`, so this becomes an ordinary
tool invocation failure with no successful observation. Subsequent calls may
succeed, if the runtime can publish and execute funded recovery work. The marker
simulates backend behavior; it cannot create tasks, restore allowance, grant
readiness or accept results. Unset the variable for ordinary use.

`tool_scope` grants permission to call tools; it is not a universal requirement
to call them. This workload states its procedure in the Skill and uses explicit
output shape and independent acceptance policies. Opaque business JSON from
other tools keeps its existing semantics unless their adapters declare a schema
or raise an execution error. The sample digest checks the two adapters' data
handoff; it is not a signature or a proof of real-world truth.

Test fixtures use explicitly configured local Workers, separate from these
production LLM profiles. Fixture, real provider and installed-artifact evidence
are reported separately. Two identities using the same model do not establish
independent people or eliminate shared model errors. This example's mechanism
and bounded sample do not qualify arbitrary autonomous research or CatAgent3.
