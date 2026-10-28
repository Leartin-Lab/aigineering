# Runtime-compiled AI4S literature example

This example publishes only a Skill, one root task, and a declarative Worker
fleet. The root Worker requests `/plan`; planning tasks compile the retrieve,
screen, extract, and synthesis graph through ordinary signed Candidates. Every
child is independently routed, claimable, recoverable, and rebuildable from
SQLite. The example contains no hand-written DAG driver.

From an empty working directory:

The fleet loads `examples.ai4s.tools` from the checkout, so keep the checkout
root on `PYTHONPATH` even when Aigineering is installed. When running the CLI
from source, also add its `src` directory. For example:

```bash
export AIGINEERING_CHECKOUT=/path/to/aigineering
export PYTHONPATH="$AIGINEERING_CHECKOUT:$PYTHONPATH"
# Add this line when Aigineering is not installed:
export PYTHONPATH="$AIGINEERING_CHECKOUT/src:$PYTHONPATH"
```

The fixture and example files below are referenced by their absolute checkout
paths so the working directory can remain empty.

```bash
export DEEPSEEK_API_KEY=replace-me
export AIGINEERING_AI4S_OPENALEX_FIXTURE=/path/to/aigineering/examples/literature-evidence/assets/openalex-response.json
mkdir -p workspaces/ai4s
aig -C workspaces/ai4s run --config /path/to/aigineering/examples/ai4s/run.toml \
  --wait-timeout 300 --json
# Use the returned contract_id for continuation or audit:
aig -C workspaces/ai4s run --config /path/to/aigineering/examples/ai4s/run.toml \
  --resume TASK_ID --wait-timeout 300 --json
aig -C workspaces/ai4s task audit TASK_ID --json
```

The two LLM profiles intentionally use the same replaceable model in the
checked-in template; capabilities and pools, not model names or prices, are the
routing contract. Operators may point the profiles at different compatible
models without changing task semantics.

For deterministic offline tool execution, keep the bundled OpenAlex fixture
environment variable. Without it, the explicitly configured tool adapter calls
the live OpenAlex Works API. API keys never enter task Assets or prompts.

`audit.py` remains an optional independent domain verifier for experiments with
acceptance policies. It is not an orchestration driver and is not required for
the runtime-compiled path above.

The preset paths are relative to `run.toml`; its database is relative to the
selected workspace. Use a fresh workspace for an independent run; a new start
refuses historical outputs. Resume does not reload inputs or reset a failed root.
See [configured runs](../../docs/reference/configured-runs.md). The entrypoint
migration has deterministic verification; the earlier linked live reports do not
constitute new provider execution of this preset.
