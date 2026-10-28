# Documentation

Start with [English](../README.md) or [中文](../README.zh-CN.md).

## Use Aigineering

| Goal | Read |
| --- | --- |
| Understand current support and limits | [Capabilities](reference/development-status.md) |
| Start a configured task with Skills and tools | [Configured runs](reference/configured-runs.md) |
| Operate the CLI task board and handle failures | [Task operations](reference/task-board.md) |
| Connect an existing Agent Harness | [Harness integration](reference/agent-harness-migration.md) |
| Import and associate files | [Attachments](reference/task-attachments.md) |
| Run a persistent local Fleet | [Fleet service](reference/local-fleet-service.md) |
| Find a runnable example or its outcome | [Examples](../examples/README.md), [case reports](../reports/README.md) |

Agents should start with the short [operating guide](../SKILL.md), then load only
the reference needed for the current task. A link is navigation, not required
context for every invocation.

## Technical reference

[DESIGN](../DESIGN.md) owns current architecture; [boundary invariants](boundary-invariants.md)
define runtime guarantees. [ADRs](adr/) explain durable decisions, while
[changes](../changes/) record implemented public compatibility and behavior.
[Conformance](../conformance/) defines protocol interoperability. These are
on-demand references, not the normal getting-started path.

For evaluated procedures, read [methods](methods.md). Contributors use
[CONTRIBUTING](../CONTRIBUTING.md); reconstruction checks are described in
[release evidence](reference/release-evidence.md). [ROADMAP](../ROADMAP.md) states
current priorities rather than a development diary.
