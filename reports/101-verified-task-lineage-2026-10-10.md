# Verified task lineage development verification

Status: bounded development verification; stable qualification remains open.

`aig task lineage ID [--json]` now navigates exact published containment and
Worker invocation-failure recovery publications from either endpoint. Each
Contract retains its ordinary task state; relation proof grants no readiness,
authority or output acceptance. See [change 080](../changes/080-verified-task-lineage.md).

## Tested snapshot and evidence

The tested source is the uncommitted `0.5.13.dev0` development worktree over
`01be3f4e585acd7975284a38e88dbdcbb44724c0`. The exact runtime module map,
artifact hashes and bounded results are recorded in
[verification data](data/101-task-lineage-verification.json). Evidence applies
to that snapshot; the Git revision alone does not identify these local changes.

The environment used Python 3.11.15 and a dedicated native Redis 7.4.11 instance.
The wheel was installed into a source-external virtual environment. Third-party
dependencies were shared from the verification environment, excluding editable
path files; this does not test a clean dependency installation from an index.
All 228 runtime module hashes matched source, wheel, sdist and installed bytes.
The console script's exact bytes, path and interpreter were checked separately.

| Scenario or command | Expected and observed result |
| --- | --- |
| Independent focused review and tests | 99 tests passed, including both declaration orders for an early pre-failure publication followed by an ordinary failed invocation and later recovery. Only the later publication forms a verified edge; the early one remains an issue. |
| Full `pytest -q` | 2514 tests passed; six dependency warnings were retained. Dedicated native Redis was enabled for the lineage operational fixture. |
| Ruff check and format check | Both passed across runtime and tests. |
| `python -m build --no-isolation` and `twine check` | Wheel and sdist built; metadata checks passed. |
| Installed CLI, signed failure fixture | One ordinary fixture invocation failed and produced a root-null recovery. Source and held recovery queries returned the same one-edge component with their existing failed/blocked states. Human output retained exact evidence references. |
| Missing task and unavailable Redis | Nonzero exits and explicit errors were observed. The fresh read-only continuation records the selected unavailable loopback endpoint. |
| Read-only and reconstruction | Every original SQLite table and schema fingerprint matched before and after inspection. A private backup rebuilt to the same semantic digest and task/edge view; the original remained unchanged. |
| Installed persistent Fleet | Three ordinary fixture tasks executed across two service generations; final output export matched and the service stopped. This is infrastructure evidence with no provider execution. |

The first installed control retained a failing checker assertion because the
canonical Redis error also included helpful message text. A subsequent read-only
checker attempt confused the initialization name with the returned Genesis ID
and stopped before querying. Both failures were retained. The corrected checker
continued against the same Store without repeating task setup, claiming or
invocation. Its five read commands, fingerprint and reconstruction checks passed.
The original failed run did not retain its read-only baseline or outage URL;
those guarantees come from the later captured read-only continuation.

## Limits and remaining gates

All execution in this report used explicit signed fixtures: no real provider
request or useful independently accepted model report is claimed. Handoff,
revision, retry, other recovery families and passive tool continuation still
need verified relation readers. Folding, hundreds-node management, deep
fact-dependent work, nested planning and the full lifecycle goal remain open.

The installed bulk priority check retained its accepted pending result, then
classified a missing target as an unknown `ValueError` and stopped later work.
The mixed-result gate remains failed pending a narrow missing-target fix and
exact recovery; the successful Fleet check does not qualify bulk operations.
No commit, remote update, tag or package publication was performed.
