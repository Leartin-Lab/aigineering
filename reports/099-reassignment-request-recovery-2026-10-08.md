# Reassignment request recovery verification

Status: bounded development verification passed; broader stable gates remain open.

Routine `task reassign --current` captures all selected original assignment
heads and saves complete private inputs before publishing. `--resume REQUEST_ID`
reuses the saved mode, Worker, reason, targets, heads and base through the same
signed assignment owner. Expert explicit single and mapped bulk forms remain
supported. See [change 069](../changes/069-reassignment-request-recovery.md).

Author checks passed 101 focused tests on the corrected source, covering the new entry and adjacent
assignment, reassignment and pending-result behavior; Ruff and format passed.
Those checks use isolated SQLite and controlled collaboration seams. They are
author self-checks, not independent review or native Redis evidence. Test-first
missing-entry failures and a later test-assertion failure retain original private
stdout, stderr and exits.

Independent review of the earlier source passed 118 focused cases but found
an uncovered cleanup defect: an exclusive temporary-file creation collision
could delete the pre-existing file. A deterministic independent probe reproduced
it. The original source and failed regression are preserved. The corrected
implementation removes only a successfully created temporary file with matching
device/inode; its collision regression and author checks pass. Independent delta review passed 21 focused request/ownership cases and the
collision sentinel control. The earlier passing suite remains evidence for its
original source rather than qualification of later code.

The current source snapshot contains 570 verification/source files and 218
runtime files. Its full suite passed 2360 tests with six existing dependency
warnings. Runtime, tests, helper and all public maps stayed byte-identical during
the run. Global Ruff/format passed; the documentation governance checkpoint
passed 48 cases. Independent helper V2 static/pure review passed after adding
independent canonical request-ID and exact base/Contract/head key recomputation.
The original insufficient helper oracles and their exact bytes remain retained.

A separate source-only revocation probe resumed a saved request after a signed
revocation of the assignment publisher key: it exited nonzero with
`outcome_unknown`, added no assignment fact and preserved the request bytes.
This is an observable unknown-stop result, not a known-rejection classification.
These controls use isolated SQLite with mocked collaboration; source checks and
native Redis tests in the full suite do not replace installed execution.

Build 1 and archive inspection passed: both archives match all 218 runtime
Python files; the source distribution includes all 199 public Markdown documents
and 22 public data files. Broad archive privacy controls and 85 direct root-guide
references passed. First outside-source installation succeeded, but the installed
helper stopped at its first origin check before domain/task setup. Its supplied
virtual-environment parent used a directory alias while the reported prefix used
the canonical path; the interpreter was not a symlink in that run. This is a
helper-oracle failure, not an operational task or provider failure. A separate
pure control also covers virtual-environment interpreters linked to an external
base interpreter.
Original binary stdout/stderr and the one-phase manifest are retained.

Helper V3 origin correction passed independent static/pure review, and build 2
and complete archive inspection passed. The second fresh installed run passed
the origin guard and all eight commands reached expected zero exits. It then
stopped because the helper parsed the accepted expert single-assignment response
as a batch envelope. The actual response contains the accepted assignment in
flat `data`; product compatibility remains unchanged. No current-head request
had run yet. Original binary streams and the failed manifest are retained; a
helper-only response-shape repair awaits independent review.

The helper V4 response-shape repair passed independent static review. The third
fresh installed run completed 125 phases with their expected exits, including
current-head set/clear operations and the deliberate save-before-publication
process interruption. Phase 126 correctly refused the helper's attempt to
reassign a task to its current Worker; the fixture expected a new head from
this no-op. Original streams, the failed manifest and saved request remain
retained. A later bare-ID versus filename comparison defect was found statically
before that phase ran. Both repairs are confined to the helper; the product's
no-op refusal remains unchanged. These partial controls are not completion of
the installed recovery matrix.

The fourth fresh installed run passed all 181 phases with ten deliberately
nonzero exits. Its 13 ordinary task fixtures produced eight saved requests;
root verification recomputed every request digest and 17 returned target keys
from the saved base, Contract and original head. All 366 outer/inner binary
streams retain verified hashes and private permissions. The runtime is the
same 218-file map tested by the full suite; only the packaged helper changed
after that suite. Helper V5 independently passed static/pure review before
this execution. The wheel SHA-256 is
`1173c46b20e799e7e00f9c3acf6ea1aec7a727e328e000d5e0235994c4fa8b63`.

Installed controls cover current set/clear, a clear head followed by a new set,
global no-history and invalid-input refusal, corrupt/missing/symlink request
refusal, cross-domain and copied-domain/path refusal, the deliberately interrupted
real save seam, stale unattempted members, replay after a later assignment and
an explicit fresh request. A selective relay fault retained the first accepted
assignment and original seeded siblings; restoring relay and resuming the exact
request replayed that fact and published the two untouched members. Three exact
native feed matches were then verified. Human recovery hints were checked with
a workspace containing spaces. Read controls fingerprint all 18 SQLite tables;
reopen/rebuild preserved assignment projection, runtime records and materialization
digest. The process interruption and relay fault are controlled fixtures, not
an uncontrolled concurrent race or a Redis server shutdown.

No actual 64-target request, installed key-revocation control or new concurrent
writer race was run. The source-only revocation probe above remains separate.
No Worker invocation, real provider request, business output or stable
qualification is claimed here. Earlier report 098 and real report 091 remain
bound to their own runtime snapshots.

Saved request files hold input context, never authority or progress state. The
request ID detects changes at the original digest-named file; a recomputed new
file is different input, not authenticated original intent. Path binding refuses
relocation and ordinary cross-workspace copying; it does not protect against
coordinated local replacement at the same domain/path. These limitations remain
explicit alongside the current signed publisher and writer fences.

Broader Kanban and stable-release gates remain open. Delivery is local edits;
no commit, remote update, tag or package publication occurred.
