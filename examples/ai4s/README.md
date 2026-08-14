# Runtime-compiled AI4S literature example

This example publishes only a Skill, one root task, and a declarative Worker
fleet. The root Worker requests `/plan`; planning tasks compile the retrieve,
screen, extract, and synthesis graph through ordinary signed Candidates. Every
child is independently routed, claimable, recoverable, and rebuildable from
SQLite. The example contains no hand-written DAG driver.

From an empty working directory:

```bash
export DEEPSEEK_API_KEY=replace-me
export AIGINEERING_AI4S_OPENALEX_FIXTURE=/path/to/aigineering/examples/literature-evidence/assets/openalex-response.json

aig domain init
aig skill load /path/to/aigineering/examples/literature-evidence
aig asset add --name literature_query \
  --content-file /path/to/aigineering/examples/ai4s/literature-query.json --json
aig task create --name ai4s_literature_report \
  --description-file /path/to/aigineering/examples/ai4s/task-description.txt \
  --input literature_query --activation literature_query \
  --output literature_report \
  --budget 24 --tool openalex_search \
  --label _skill_content_literature_evidence \
  --requires-capability planning --worker-pool reasoning \
  --delegate-capability literature.retrieve \
  --delegate-capability literature.screen \
  --delegate-capability literature.extract \
  --delegate-capability literature.synthesize \
  --delegate-capability literature.replan \
  --delegate-capability literature.verify \
  --delegate-pool economy --delegate-pool reasoning \
  --delegate-pool verification \
  --acceptance-policy '{"mode":"independent","policy_version":"ai4s-literature-v1","required_attestations":1,"verifier_capabilities":["literature.verify"],"output_shapes":{"literature_report":{"answer":"nonempty_string","citations":["nonempty_string"],"limitations":["nonempty_string"]}}}' \
  --json

aig fleet run --config /path/to/aigineering/examples/ai4s/workers.toml \
  --task TASK_ID --wait-timeout 300 --json
aig task audit TASK_ID --json
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
