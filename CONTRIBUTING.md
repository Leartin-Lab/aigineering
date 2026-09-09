# Contributing

Aigineering is an early Zero Trust Agent Runtime. The main rule is to preserve
the runtime boundary: worker output is a candidate until authority projects it
into a runtime fact.

## Branch Flow

- `main` is the protected release branch.
- `dev` is the integration branch for ongoing work.
- Feature work should branch from `dev`.
- Changes to `main` should go through a pull request.
- Pull requests should pass CI before merge.

## Local Checks

```bash
pip install -e ".[dev]"
ruff check src/aigineering tests
ruff format --check src/aigineering tests
pytest -q
python -m build
```

The CI matrix currently runs Ubuntu with Python 3.11, 3.12 and 3.13. Local
Windows checks must preserve functional assertions while skipping POSIX-only
mode-bit checks; Windows ACLs are not equivalent to Unix `0600` permissions.
Symlink tests check whether the platform permits creating a link. Subprocess
tests use the active Python interpreter, and installed-wheel smoke selects
`Scripts/*.exe` on Windows or `bin/*` on POSIX. These accommodations do not
constitute a Windows CI certification.

## Architecture Guardrails

- Do not let worker output directly mutate shared state.
- Do not treat undeclared outputs as committed assets.
- Record rejected candidates in trace.
- Keep DAG/tree views as trace projections, not runtime primitives.
- Treat action requests such as `/plan`, `/replan`, `/retry`, and `/tool` as
  explicit ordinary Candidate/Contract work compiled through the relevant
  Plugin or WorkerHost, never as hidden controller state. See ADR-011 and
  `DESIGN.md` for the current implementation boundary.
- Keep worker pull/submit claim-bound and transactionally committed on the
  SQLite path.
