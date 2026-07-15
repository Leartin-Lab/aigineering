# ADR-008: Unified Runtime Ingress

**Status:** Accepted
**Date:** 2026-06-23
**Scope:** v0.5.0 delivery-blocking
**Related:** ADR-001, ADR-003, ADR-005, ADR-007, internal ADR-031, ADR-039, ADR-042, ADR-043

## Context

In the pre-v0.5.0 codebase, runtime facts (assets, contracts, candidate
submissions, replacement claims) entered the system through multiple
independent surfaces:

- `Engine.add_asset()` and `Engine.add_contract()` wrote directly to store
- `Engine._commit()` wrote accepted projection results directly
- `MethodRuntime.add_contract()` and `mint_system_asset()` wrote directly
- `control_plane.inject_asset()` and `inject_contract()` had their own
  signing, authority-check, and store-write paths
- CLI commands (`aig asset`, `aig contract`, `aig capability`, `aig mcp`,
  `aig skill`, `aig behavior`) each wrote directly to store
- Server endpoints (`slice_asset`, replacement-claim creation) wrote
  directly
- `skill_loader` wrote descriptor assets directly
- `labels` created placeholder assets directly
- `submit_candidate` had its own commit path

Each path carried its own assumptions about signing, protected-name checks,
authority decisions, trace recording, and reducer invocation.  Some paths
ran all checks; others ran a subset.  It was possible to add an asset to
the store without any trace record, and it was possible to create a
contract with reserved output names without rejection.

## Decision

All runtime facts enter the system through a single **RuntimeIngress**.
This ingress is the **only** production path for creating or modifying
runtime facts.

### Ingress API

```python
class RuntimeIngress:
    def accept_asset(asset, *, source, allow_protected=False) -> Asset
    def accept_contract(contract) -> Contract
    # Candidate output is not a generic ingress fact. It must use the
    # claim-bound submit_candidate(envelope, ...) operation.
    # future: accept_replacement_claim(...), accept_control_fact(...)
```

Every accepted fact passes through the same pipeline:

1. **Sign** — produce or verify a deterministic provenance seal
2. **Protect** — enforce reserved-namespace rules (`_sys_`, `_tool_obs_`,
   `_memory_`, etc.)
3. **Commit** — persist to the store (the single store-write primitive)
4. **Trace** — append an immutable audit record
5. **Reduce** — invoke `FactReducer.on_asset_created()` to project
   consequences (activation readiness, output satisfaction, contract
   completion, child cancellation)

### Allowed Direct-Store Write Exceptions

The following may call `store.add_asset` / `store.add_contract` directly:

- Store implementations (`store.py`, `sqlite_store.py`)
- The `RuntimeIngress` itself
- Test fixtures (under `tests/`)

There is no store-agnostic transaction helper. Operational commits require a
store implementation that provides the complete atomic ingress operation;
buffering several direct writes in Python is not transactionality and is not a
supported fallback.

All other production code must route through `RuntimeIngress`.

### Why Different UX Is Still Allowed

CLI commands (`aig asset add`, `aig contract add`), server endpoints, and
programmatic callers may keep their user-facing interfaces.  But their
implementations must call `RuntimeIngress.accept_*` methods, not touch
`store.add_*` directly.  The user experience does not change; the
mutation semantics become identical across all surfaces.

## Consequences

### Positive

- Every runtime fact has an immutable audit trail (trace entry)
- Protected-name enforcement is centralized — no surface can accidentally
  allow reserved names
- Static analysis can verify that no production code bypasses the ingress
  (the `test_no_direct_store_write_in_production` test enforces this)
- Reducer invocation is guaranteed for every asset
- New CLI/server/programmatic surfaces cannot reintroduce inconsistent
  authority or trace behavior

### Negative

- CLI and server code depend on `RuntimeIngress` and a StorePort with explicit
  transactional ingress operations
- Adding a new asset/contract surface requires explicitly threading the
  ingress through

## Verification

```bash
# Production direct-write ban (static)
rg "\.add_asset\(|\.add_contract\(" src/aigineering/ --include="*.py" -l \
  | rg -v "(test_|__pycache__|store|ingress|RuntimeIngress|_store\.py|transaction|fixture)"
# Expected: empty output

# Every ingress path produces uniform trace records
pytest tests/ -k "ingress and trace_shape"

# Idempotency: duplicate acceptance rejected
pytest tests/ -k "ingress and idempotent"
```
