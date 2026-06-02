# Contributing

Aigineering is pre-alpha. The main rule is to preserve the runtime boundary:
worker output is a candidate until authority projects it into a runtime fact.

## Branch Flow

- `main` is the protected release branch.
- `dev` is the integration branch for ongoing work.
- Feature work should branch from `dev`.
- Changes to `main` should go through a pull request.
- Pull requests should pass CI before merge.

## Local Checks

```bash
pip install -e ".[dev]"
pytest -q
python -m build
```

## Architecture Guardrails

- Do not let worker output directly mutate shared state.
- Do not treat undeclared outputs as committed assets.
- Record rejected candidates in trace.
- Keep DAG/tree views as trace projections, not runtime primitives.
- Treat method requests such as `/plan`, `/replan`, and `/fail` as future
  explicit subtasks, not hidden controller operations.
