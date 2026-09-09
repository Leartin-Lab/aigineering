# Reusable methods

v0.5.11 provides a local method lifecycle over existing signed Assets, Contracts,
Worker claims and independent acceptance. A method is a versioned procedure with
an explicit interface. Importing it does not install code or make it trusted.

## Package format

A `method-package-v1` JSON file contains:

- `name`, `version`, `description`, `instructions`;
- `inputs`: unique logical input slots;
- `outputs`: logical output slots mapped to deterministic output shapes;
- `requirements`: `tool_scope`, `worker_capabilities`, `worker_pools`,
  `delegation_capabilities`, `delegation_pools`;
- `dependencies`: exact method Asset IDs already available in the domain;
- `context_asset_ids`: exact supporting Asset IDs;
- optional `examples`: author-visible input/expected-output examples.

Shapes use the existing acceptance language: `string`, `nonempty_string`,
`number`, `boolean`, exact-key objects, or a one-element array describing a
nonempty homogeneous array. A `null` shape allows arbitrary text, useful for
method-creation outputs whose package structure is validated by the consumer.
JSON declarations are limited to 256 KiB and depth 32. Input/output/example and
suite case counts are bounded at 32. Duplicate and unknown fields, floats, unsafe
integers and normalized duplicate keys fail closed.

Package version labels are descriptive; execution and evaluation bind exact
Asset IDs. Required tools must be explicitly authorized at invocation. Installing
another tool, changing routing profiles or authorizing an actor remains a separate
operator action. Package dependencies must remain within the root requirements.

## Import and invoke

```bash
aig domain init
aig method import method.json --json
aig method inspect METHOD_ASSET_ID --json
aig asset add --name source --content 'input material' --json

aig method instantiate METHOD_ASSET_ID \
  --input source=INPUT_ASSET_ID --output assessment=assessment \
  --name first-trial --budget 4 --allow-unverified --json

aig run --task CONTRACT_ID
```

Add `--allow-tool TOOL_NAME` for each required tool. The tool must also be
configured through the normal ToolRegistry/Fleet path. Requirements select
eligible Workers; they do not install Worker implementations.

Default instantiation requires `--evaluation QUALIFIED_EVALUATION_ASSET_ID`.
`--allow-unverified` is an explicit trial authorization and is recorded in the
Contract. It does not disable core authority, output-shape or claim checks.

Exact input IDs are frozen in `context_asset_ids`, with logical slot bindings in
the Contract description. Later assets sharing the same name cannot replace them.
Redacted, tombstoned or nonpromptable dependencies are rejected. The CLI is a
local administrative surface, not a multi-user authorization service.

## Create or revise a method

```bash
aig asset add --name method_request \
  --content 'Create a bounded method that checks source and reported counts.' --json

aig method create --request REQUEST_ASSET_ID --output proposed_method \
  --name create-count-check --budget 4 --json

aig run --task CREATION_CONTRACT_ID
aig asset ls --json
```

The seed authoring method is published as an ordinary package. The creation task
uses the configured LLM Worker; `create` itself never calls a model. A returned
package remains an unverified assertion. Existing harnesses can produce the same
JSON through their canonical signed `/exec` output.

For a revision, publish a new request and pass `--context OLD_METHOD_ASSET_ID`
and `--context FAILED_EVALUATION_ASSET_ID`. Both exact assets become visible to
the new task. The old method and its failed evidence are retained. Revisions do
not inherit another package's evaluation.

## Test, assess and accept

A separate suite has this structure:

```json
{
  "schema": "method-cases-v1",
  "name": "count-checks",
  "cases": [
    {
      "name": "matching",
      "inputs": {"source": "{\"source\":3,\"reported\":3}"},
      "expected": {"assessment": {"matched": true}}
    },
    {
      "name": "mismatch",
      "inputs": {"source": "{\"source\":3,\"reported\":4}"},
      "expected": {"assessment": {"matched": false}}
    }
  ]
}
```

```bash
aig method cases cases.json --name count-cases --json
aig method test METHOD_ASSET_ID --cases SUITE_ASSET_ID \
  --name evaluation-one --budget 2 --json

aig run --task TEST_ROOT_CONTRACT_ID
# Heterogeneous methods can instead use the normal configured Fleet.
aig method assess RUN_ASSET_ID --json
aig method show EVALUATION_ASSET_ID --json

# Explicit reviewer decision after inspecting the evidence:
aig method attest EVALUATION_ASSET_ID --verdict accepted --json

aig method instantiate METHOD_ASSET_ID \
  --input source=NEW_INPUT_ASSET_ID --output assessment=new_assessment \
  --name evaluated-reuse --budget 2 --evaluation EVALUATION_ASSET_ID --json
```

`test` prepares ordinary case tasks and a root obligation; it does not execute a
private loop. Case Workers receive input contents, method instructions and exact
dependencies, not the suite's expected answers. `assess` uses a deterministic
Worker to publish exact-comparison evidence. Assessment is not independent
acceptance: an authorized actor with `method.verify` must attest the exact report.
The CLI reviewer uses a separate local key; it is still controlled by the local
operator and is not evidence of an independent organization or model.

An unfinished suite cannot be assessed. A failed/cancelled root or unsuccessful
producer cannot produce passing evidence. A sibling recovery outside the original
case lineage requires a new suite run. Failed assessments remain inspectable and
can be supplied as revision feedback; they cannot authorize default reuse.

Test plans, outputs, reports, attestation and eligibility are reconstructed from
SQLite. A copied report or a forged `passed` field is insufficient: reuse
recomputes the test evidence and requires the exact independent qualification.
Passing fixtures is bounded evidence, not a general semantic-correctness claim.

## Scope

The standalone `examples/method-governance` fixture demonstrates the complete
protocol lifecycle without provider credentials. Real-provider productivity and
quality comparisons remain separate experiments for the v0.6.0 evidence work.

There is no remote package market, automatic download, arbitrary generated-code
execution, automatic reviewer selection, risk taxonomy, multi-party quorum or
mutable global trust registry. Existing Skills remain supported; no migration of
`skill.toml` is required. Methods add no core effect types or database migration.
