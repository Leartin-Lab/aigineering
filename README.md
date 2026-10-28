# Aigineering

[English](README.md) · [中文](README.zh-CN.md)

**Zero Trust agent runtime for auditable AI-assisted engineering and research.**

Aigineering gives existing Agent Harnesses a durable boundary for delegated
work: explicit inputs and outputs, bounded permissions, claim-bound execution,
and policy-bound acceptance. Worker output is an untrusted Candidate;
authenticated, authorized effects become durable facts through transactional
commitment.

Content-addressed Assets fix the versions being checked. Recorded lineage helps
you revisit evidence and revise questionable results without reconstructing a
model's private reasoning. Content correctness still needs appropriate checks.

## What you can use

- Run a task from explicit files, or a configured fleet with Skills and tools.
- Let `/plan` and `/replan` expand work into ordinary, bounded runtime tasks.
- Inspect the CLI task board, blockers, claims, evidence and recovery successors.
- Review exact output Assets and export results that satisfy the task's policy.
- Keep an existing Harness's model and tool loop behind the signed Worker API.

Current source: **v0.5.14**, for bounded local single-machine CLI workflows.
SQLite owns facts and claims; **Redis is required** for application discovery,
event feeds and wakeups. Version metadata alone does not qualify a delivery:
claims depend on evidence for the applicable case. General unattended delivery,
production MCP transport and distributed deployment are outside current support.
See [support and limits](docs/reference/development-status.md).

## Start with one task

Requires Python 3.11+ and Redis. Use the source checkout for the supplied examples.

```bash
git clone https://github.com/Leartin-Lab/aigineering.git
cd aigineering
python -m venv .venv
source .venv/bin/activate
pip install -e .
docker compose up -d redis
aig redis status --json

export AIGINEERING_API_KEY="your-provider-key"
export AIGINEERING_MODEL="your-model"
export AIGINEERING_BASE_URL="https://your-provider.example/v1"

mkdir -p workspaces/quick
aig -C workspaces/quick run "Summarize the supplied measurements and their limitations." \
  --input sample="$PWD/examples/cli_research/sample.json" \
  --output report.md --timeout 60 --max-retries 1 \
  --max-output-tokens 8192 --json
```

Use a compatible model that can follow the Worker action protocol. Replace the
provider settings explicitly; keep credentials in environment variables. The
report is written to `workspaces/quick/report.md`; existing files are not
overwritten. This quick path does not grant tools or independent acceptance.

Use the returned `contract_id` to inspect the actual task:

```bash
aig -C workspaces/quick task status TASK_ID --json
aig -C workspaces/quick task board --json
aig -C workspaces/quick task audit TASK_ID --json
```

## Planning, tools and independent acceptance

The [CLI research example](examples/cli_research/README.md) starts from one root
task, frozen input and a Skill. The model plans the work; two tools compute a
sample mean and unit conversion, then a different authorized Worker checks the
exact report. There is no example-owned task scheduler.

Its preset selects `deepseek-flash`; edit `workers.toml` for another compatible
provider. The example adapter must also be importable:

```bash
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p workspaces/research
aig -C workspaces/research run \
  --config "$PWD/examples/cli_research/run.toml" --wait-timeout 300 --json
```

Skills supply selected context, not authority. `aig mcp add` registers capability
metadata; it does not connect to an MCP server. Keep your Harness's MCP client or
supply an explicit tool/Worker adapter. See [configured runs](docs/reference/configured-runs.md)
and [Harness integration](docs/reference/agent-harness-migration.md).

## When work stops

Read `task status` first. Use `task runs`, `diagnostics`, `lineage` and `logs` only
for the relevant task and claim. Submission, tool observations and scheduled
recovery are not business completion. Continue an eligible task with the same
configuration and `--resume TASK_ID`; a published recovery successor has its own
ID. Do not rerun a submitted producer just because acceptance is pending.

`--timeout` bounds one provider request; `--wait-timeout` is for target/configured
runs, not quick-goal execution.
Provider retries and new runtime recovery tasks are different. `--json` selects
CLI output; it does not select the provider's JSON response mode. Commands have
their own result schemas and exit codes. See [task operations](docs/reference/task-board.md).

## Read next

- [Agent operating guide](SKILL.md): a short loop and references to load on demand.
- [Examples](examples/README.md) and [case reports](reports/README.md): inputs, outcomes and limits.
- [Documentation](docs/README.md): configuration, task operations and integration.
- [Roadmap](ROADMAP.md) · [Contributing](CONTRIBUTING.md) · [License](LICENSE).
