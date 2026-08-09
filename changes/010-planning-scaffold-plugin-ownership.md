# Change 010: Planning scaffold Plugin ownership

Status: Implemented and verified on dev; release pending
Target: v0.5.3

## Problem

Planning scaffold parsing, validation, and symbolic compilation live under
`core/`, while the only production consumer and the remaining plan semantics
live under `plugins/`. This makes a feature-specific language appear to be a
runtime-kernel primitive and splits one semantic owner across packages.

## Intended change

- move the scaffold model/parser/validator/compiler under `plugins/`;
- keep generic activation, authority, identity, and Candidate commitment in
  `core/`;
- preserve wire payloads, Contract identities, validation findings, and public
  runtime behavior;
- add an architecture constraint preventing planning semantics from returning
  to the kernel.

## Non-goals

- changing planner prompts or scaffold schema;
- merging planning stages back into one invocation;
- rewriting `task_semantics.py` in the same mechanical move;
- removing documented compatibility imports without deprecation evidence.

## Exit criteria

No production planning implementation is owned by `core.plan_scaffold`, all
planning and long-chain tests remain equal, and the move reduces ownership
ambiguity without adding adapters or duplicate code.

## Implementation evidence

- the 526-line scaffold implementation moved mechanically from `core/` to
  `plugins/`, with the sole production import updated;
- the duplicated plan-specific reserved-prefix set now has one Plugin owner;
- no Store, runtime coordinator, or SQLite dependency entered the moved module;
- architecture tests forbid the old core owner and the duplicated prefix rule;
- focused planning, containment, WorkerHost, Engine Worker, and CLI Worker
  tests passed; Ruff and the full suite passed with 1130 tests and 3 skips.
