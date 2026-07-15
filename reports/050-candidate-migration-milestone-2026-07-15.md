# 0.5 Candidate migration milestone evidence

Commit under test: `605fdaf` (`dev`)
Date: 2026-07-15
Environment: macOS, Python 3.11.15, SQLite reference Store
Scope: Change 001 through signed Contract/Task/Asset/Behavior publication

## Claims supported by this evidence

- A Store can persist and reconstruct one immutable Genesis trust root.
- Local CLI publication uses an Ed25519 actor key; deterministic content seals
  are rejected as Candidate authentication.
- `aig contract add`, `aig task create`, `aig asset add`, and
  `aig behavior add` publish typed signed Candidates through one commitment
  coordinator.
- The coordinator commits receipt, accepted/rejected decision records, facts,
  trace evidence, and FactReducer consequences transactionally on SQLite.
- Repeating an already recorded Candidate does not advance runtime revision.
- Asset commitment preserves activation/completion behavior.
- Deleting and rebuilding runtime materializations preserves the runtime digest
  in the existing reconstruction acceptance scenario.
- Effect payload semantics are outside the commitment coordinator; an
  architecture gate prevents effect-name branches and caps the coordinator at
  fewer than 300 lines.

## Verification

Commands:

```text
pytest -q
ruff check src/aigineering tests
ruff format --check src/aigineering tests
python3 -m build
unzip -t dist/aigineering-0.5.0-py3-none-any.whl
```

Observed:

- `1070 passed in 35.89s`
- Ruff check passed.
- 190 Python files matched formatter output.
- Built `aigineering-0.5.0.tar.gz` and
  `aigineering-0.5.0-py3-none-any.whl` successfully.
- Wheel archive integrity check reported no errors.
- Metadata reports version `0.5.0`, Python `>=3.11`, and runtime dependencies
  `click>=8.0` plus `cryptography>=43`.
- Candidate, Genesis, commitment, effect projection, and domain CLI modules are
  present in the wheel.
- Legacy `core/engine.py`, `core/startup_check.py`, and
  `core/state_serializer.py` are absent from wheel and sdist.

## Important limitations

This is a migration milestone, not 0.5 release acceptance.

- Worker raw-output CandidateEnvelope submission has not yet converged with
  CandidateProposal and actor-key registration.
- Asset slice, capability, MCP, provider configuration, recovery, server, and
  several demo/setup paths still use compatibility RuntimeIngress.
- Method runtime/registry/handlers are still shipped; plan/replan/recovery/tool
  have not yet become ordinary plugins.
- Candidate idempotency lookup currently scans RuntimeRecords and needs a
  Store-level indexed projection before load testing.
- Cross-process concurrent Candidate commitment has not yet received a dedicated
  race/crash-injection acceptance test.
- No live-LLM productivity claim is renewed by this milestone.

