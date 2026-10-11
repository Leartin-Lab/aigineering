# Unified configured-run entry verification

Status: Local development verification passed; no stable-release qualification
Date: 2026-10-03
Baseline: 01be3f4e585acd7975284a38e88dbdcbb44724c0
Source version: 0.5.13.dev0

## Scope and snapshot

[Change 037](../changes/037-unified-run-entry.md) adds workspace/config/resume
entry conventions and a shared configured Fleet launch service. The existing
Candidate/fact kernel and protocol schema remain the execution authority.
[ADR-026](../docs/adr/ADR-026-shared-run-entry.md) records the decision and
[configured runs](../docs/reference/configured-runs.md) documents usage.

Verification binds the final working-tree snapshot over 328 files: Python source,
tests and scripts, `pyproject.toml`, and the four example `run.toml` presets.
Its SHA-256 is
`f360af6cf5167f3855644eed153e26a40d0612b4c6f253b89b11be58951c8b46`.
The fingerprint hashes compact sorted JSON mapping relative paths to file SHA-256
values. Documentation receives a separate final governance/reference check and
artifact byte comparison.

## Deterministic and installed evidence

| Check | Result |
| --- | --- |
| Ruff check on source, tests and installed-smoke script | Passed |
| Ruff format check on the same scope | 322 files passed |
| Serial full `pytest -q` | 1586 passed, 3 Redis integration skips, 6 existing dependency warnings; 58.10 s |
| Standard isolated `python -m build`, using cached dependencies offline | Wheel and sdist built; wheel built from sdist |
| Artifact metadata, members and source/document byte comparison | Passed |
| Fresh installation outside the source tree | Passed; import location and version checked |
| New configured fixture start, same-preset resume, direct Worker-config resume and old Fleet entry | Passed in separate installed CLI processes |
| Resume after deleting the original input file | Passed without setup reload |
| New-start rejection of an old root/output | Passed; no false fresh completion |
| Configured Store backup, reopen and reconstruction | Passed; records unchanged and semantic digests equal |
| Final public documentation governance and root guide references | Passed |

The final configured Store reconstruction digest before and after rebuild is
`ed583f2a03a23d19dd7a04de3a7ea5ca344d831689461b790a3d7ab5b2dfbc84`.
Evidence directories remain private because they contain Store snapshots.

The four example presets have preparation/resource/routing checks; they were not
executed with live providers. Exact Skill context and independent-policy source
evidence binding are tested against actual committed `asset:v1` IDs. That test
stubs Fleet execution and explicitly checks no business output was produced; it
is publication evidence, not independent business acceptance. The existing AI4S
fixture separately exercises real runtime tool continuation, independent
attestation and reopen through the shared Fleet CLI path.

The delegated implementation received requirement and architecture review.
Integration fixes and added checks were verified on the final snapshot; author
checks are not described as an additional independent review.

## Corrected verification failures

Initial combined collection failed because the architecture and service tests
had the same module basename; the architecture test was renamed. An added
historical-output regression test initially called a nonexistent Store getter;
the test was corrected to compare the durable revision. The first full run had
one failure because an old AI4S test still patched the CLI-owned Worker builder;
it now patches the shared application dependency and retains its exact runtime
assertions.

The initial isolated build could not download its backend through the network
sandbox. The first offline dependency inventory lacked `pluggy`; after adding the
cached dependency, standard isolated builds passed without network access. An
installed fixture's new reconstruction check initially ran before its output
parent existed and exited with code 2. Moving that check after evidence-directory
creation fixed the script. These were corrected collection, test-integration,
build-provisioning and verification-script failures; no runtime reconstruction
mismatch or new provider failure is inferred from them.

## Limits

There was no new live provider execution for this slice. Historical live results
belong to the v0.5.12 snapshot or earlier case reports and do not validate this
entrypoint. Installed verification uses an explicitly configured deterministic
fixture Worker, not a production default. Redis service integration remains
untested here.

Workspace selection is a path convention, not sandbox or namespace isolation.
The new-start historical-output check is a startup guard, not an atomic namespace
reservation; independent concurrent workloads should use distinct workspaces.
Setup publication and execution are separate signed transactions. Local
factories remain trusted operator-selected code. Quick-goal/single-worker loops
are retained, and method lifecycle preparation remains explicit.

This delivery contains local development edits and built artifacts only: no new
commit, remote update, tag or package publication. The next usability slices
remain exact task/recovery export and configurable quick-goal allowance.
