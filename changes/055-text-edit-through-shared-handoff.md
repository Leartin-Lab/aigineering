# Task text editing through immutable successors

Status: Bounded development verification complete; stable qualification remains open.

`task edit ID --title TEXT --body TEXT --reason TEXT` uses the shared handoff
application service and plugin. Supply one or both replacements; omitted text
stays unchanged and an explicitly empty body clears it. Text is valid UTF-8,
bounded to 65536 bytes per field, with nonblank titles and canonical Unicode
NFC no-op rejection. The reason remains audit metadata rather than appended
instructions. `task edits ID` reads the shared immutable transfer history.

The signed request cancels and fences the old attempt, then atomically declares
a bounded successor with a new ID. Ordinary eligible active and expanded
handoff classes retain their existing scope, input, output, allowance,
disclosure and independent-acceptance constraints. Assignment inherits;
priority does not. Editing does not reset claims, reopen terminal work or
qualify the new output from old evidence.

Edit signs the exact assignment head and complete source hold set. Up to 64
source holds are copied atomically with exact reasons and one-to-one provenance;
old placements remain historical facts. Descendant holds still block transfer.
The shared handoff link binds source/successor declarations and the copied map.
There is no new effect alias, scheduler, fact ledger, Store schema or reader floor.
Ordinary omitted-edit handoff wire behavior is preserved.

Explicit `task.edit` authority is added to existing cancel/publish/handoff
requirements; copying holds requires `task.block`. Exact replay proves the
original signed request and committed provenance under current authorization,
including after old-source hold cleanup. Returned data includes the actual key,
source snapshot, successor ID and mapping. Redis remains required, and accepted
facts and their result remain available when relay synchronization is pending.

See [ADR-043](../docs/adr/ADR-043-text-edit-through-shared-handoff.md) and the
[operator workflow](../docs/reference/task-board.md#edit-task-instructions).
This implementation does not by itself qualify the wider task workflow or a
stable release.
