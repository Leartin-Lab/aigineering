# Change 033: Usable quick-goal CLI inputs and results

`aig run GOAL` now treats the supplied goal as an explicit input Asset and can
load additional UTF-8 text files with repeatable `--input NAME=PATH` options.
Each run assigns unique input and output slots, and its Contract description
records the goal and the source names associated with those slots. The quick
goal path no longer publishes sample data or citation Assets; `aig demo` keeps
the sample-backed quickstart behavior.

A completed run writes its readable Markdown result to `--output PATH`, or by
default to `.aig/outputs/<session>.md`. `--json` includes output slots, Asset
IDs, available text, and the path written. Sealed, non-promptable, redacted, and
tombstoned outputs are not exported. A file-write failure is reported as an
output error while preserving the completed task and its committed Asset.

Input files are validated before opening the Store: names must be unique and
well formed, each file must be non-empty UTF-8 text no larger than 1 MiB, and
read errors are reported without publishing a Contract or Asset. Failed task
runs preserve their session and trace, return a non-zero status, and include
blockers, rejected output, recovery successor IDs, and a task-audit command in
their diagnostics. Goal, `--once`, and `--task` modes are exclusive; options
that a mode cannot honor are rejected explicitly.

The CLI also exposes `--max-output-tokens` (default `2048`), optional
`--thinking-mode enabled|disabled`, and opt-in `--response-format json_object`;
omitting either provider mode leaves provider defaults unchanged. The JSON wire
mode asks the provider for a complete JSON response, then compiles only the
canonical action wrapper under the existing strict parser. It cannot be
combined with `--tool-registry`. `--json` continues to select CLI output and
does not enable provider JSON mode. `--save-config` records these settings
without credentials.
Request timeouts retain fractional seconds and run-loop timeout values must be
finite and positive.

Quick-goal execution does not implement run-loop waiting or polling. Explicit
`--wait-timeout` and `--interval` therefore reject before task setup instead of
being silently ignored. Use `--timeout` and `--max-retries` for quick-goal
request limits, or select target/configured execution for a wait budget.

These changes remain in the CLI and application composition layer. An accepted
Worker output is an authorized assertion; this path does not claim independent
semantic acceptance or universal truth. `aig run GOAL` does not grant external
tools; use task publication and the task-pool tool registry path for tool-backed
work.
