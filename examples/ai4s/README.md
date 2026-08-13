# Auditable AI4S literature run

This example crosses the real LLM, tool, Candidate, commitment, independent
acceptance, and SQLite-rebuild boundaries. Domain semantics remain in this
adapter; the runtime kernel only sees ordinary tasks, assets, Workers, and
Candidates.

Initialize a clean working directory, publish the task, then run both the LLM
and configured tool Worker:

```bash
aig domain init
aig task create --name ai4s_literature_report \
  --description-file /path/to/aigineering/examples/ai4s/task-description.txt \
  --output literature_report --budget 4 --tool openalex_search \
  --acceptance-policy \
  '{"mode":"independent","policy_version":"ai4s-literature-v1","required_attestations":1,"verifier_capabilities":["verify.literature"]}' \
  --json

aig run --task TASK_ID --model MODEL --base-url BASE_URL \
  --tool-registry /path/to/aigineering/examples/ai4s/tools.py:build_registry \
  --wait-timeout 120 --json

python3 /path/to/aigineering/examples/ai4s/audit.py \
  --task TASK_ID --output ai4s-audit.json
aig task status TASK_ID --json
```

For deterministic offline replay, set
`AIGINEERING_AI4S_OPENALEX_FIXTURE` to the bundled
`examples/literature-evidence/assets/openalex-response.json`. Without that
variable the tool calls the live OpenAlex Works API. The configured registry
is trusted local operator code and is loaded only when `--tool-registry` is
explicitly supplied.

The mechanical verifier proves only that the report is valid JSON and every
citation ID occurs in a committed successful OpenAlex observation belonging
to the task graph. It does not prove that OpenAlex metadata is true or that the
LLM's prose inference is scientifically correct.
