# 037: Shared configured-run entry

Status: Implemented; local development verification recorded in report 071

## Problem

Complete examples required separate resource publication and Fleet launch
recipes. The Fleet command also owned composition reused by another CLI module,
so sharing the kernel did not provide one understandable application entry.

## Resulting design

`aig -C WORKSPACE run --config PRESET` prepares explicit input files, Skills,
a root Contract and existing Worker profiles, then launches the canonical local
Fleet. `run --config PRESET_OR_WORKERS --resume TASK_ID` continues committed work;
`--resume` also aliases single-worker target selection. The original `fleet run`
entry delegates to the same application service. Signed local publication and
tool descriptor publication have neutral owners outside CLI command modules.

Strict preparation rejects invalid resources and task declarations before
runtime provisioning. Published label context and configured acceptance evidence
bind actual committed Asset IDs. A new start rejects existing roots and output
slots; resume preserves the frozen task and does not republish setup, reset
claims or execute recovery successors implicitly. The four complete declarative
examples now include presets; their ORACLE files are not inputs.

Fleet profiles accept `response_format = "json_object"` for the existing LLM
JSON action mode. Configuration never grants capabilities through Skills or
labels, changes the commitment boundary, or creates a business scheduler.

## Compatibility and limits

Existing quick goals, task pools, manual publication and `fleet run` remain
available. Configuration mode rejects explicit provider/tool/mock overrides so
profiles remain the source of launch configuration. Preset resources are
preset-relative; the persistent database remains workspace-relative. Empty or
`:memory:` database paths are rejected. This workspace
convention is not sandbox isolation.

Methods retain their explicit lifecycle preparation, and Skill-only packages
are not promoted to standalone workloads. Quick-goal and single-worker execution
loops are not yet merged into the Fleet service. Interface inspiration does not
provide external harness protocol or session compatibility.

This is v0.5.13.dev0 development work, with deterministic and installed fixture
evidence in [report 071](../reports/071-unified-run-entry-2026-10-03.md). No new
provider execution, stable release, tag or package publication is claimed.
