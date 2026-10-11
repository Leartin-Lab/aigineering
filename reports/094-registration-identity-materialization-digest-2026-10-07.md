# Registration identity materialization verification

Date: 2026-10-07. Source version: **0.5.13.dev0**.
This is a bounded verification fix, not stable-release or provider qualification.

## Behavior and scope

The runtime materialization digest now includes Worker registration `actor_id`
and `key_id`. With every other field and immutable fact unchanged, either
projected identity change is detected by local integrity comparison. Rebuilding
from the original signed facts restores the identity and digest. Previously the
digest omitted these two columns.

Only the existing digest payload changed. Signed facts, wire/schema, claim,
registration publication and authority checks are unchanged. Display timestamps
remain excluded because live writes and fact-derived reconstruction may differ.
Recompute verification digests after upgrade; previous strings cover different
fields. Local consistency does not prove signatures, log completeness or an
external checkpoint.

## Source verification

The full-suite snapshot has 549 source/config/test/helper files, map SHA256
`0c760a9b8ee71af36090b46ed53700278d9ee9140450c94d5a85949b315aee87`.
Its 214 runtime files have map SHA256
`e2cabad159b61922f29402ba077883eab4ecffcd42e34bdef02cf1329ae4872f`.

- Root serial full suite: **2230 passed**, six existing dependency warnings,
  112.69 seconds, with isolated Stores and real Redis.
- Root Ruff and format checks passed; **471** source/test files checked.
- Author focused digest/integrity tests: **8 passed**. The signed baseline,
  individual identity projection changes, read-only integrity checks,
  reconstruction and reopen are tested. Author self-checks are not independent
  review and their count is not added to the full-suite count.
- Independent implementation review and complete digest, integrity and Worker
  registration test files: **11 passed** with explicitly isolated Redis. Counts
  overlap the full suite. Independent Ruff and format checks passed.

Earlier fixture setup failures and sandbox Redis connection failures are retained.
One launcher failure has only a recovered transcript; it is not original raw
stream evidence. An unexecuted helper version lacked a retained source copy;
that gap is disclosed and excluded from accepted installed evidence.

## Installed acceptance

The packaged control uses ordinary signed local registration,
without invoking a Worker. It tests actor/key separately by changing only a
private projection column, preserving immutable facts and the other fields,
then rebuilding and reopening. Those SQL changes are explicit negative fixtures,
not operational registration commands. Each integrity consumer call compares
all original SQLite tables before/after to prove it does not repair the source.

Root installed a fresh wheel outside the source tree with offline dependencies
and real isolated Redis. The independently reviewed packaged helper passed its
three phases: installed-module binding and the actor/key controls. Its SHA256 is
`561878f7b458b3b2a53db6d0c384d28230bd1711e0808ab9995ebd91dc2243c7`;
the tested wheel SHA256 is
`0695bb39d82a4e23f2a5d0072e7081993b0597127a7464bdf767ff70591d9304`.
Each case has one ordinary signed registration fact and preserves the complete
immutable record history. All eight integrity-consumer calls preserve every one
of the **18 original SQLite tables**, including sqlite_sequence.

| Changed projection field | Baseline consistent | After change | After rebuild | After reopen |
| --- | --- | --- | --- | --- |
| actor_id | true | false | true | true |
| key_id | true | false | true | true |

Root also ran two fresh negative controls on the retained earlier wheel, binding
the old sqlite_store source SHA256
`6d59759456ca17593e096527e0f4a0d4e8bae7b58eb43a082154299ef39e470c`.
Both preserve facts but leave the old digest unchanged and incorrectly report
local consistency. Their expected failing exits and original streams are
retained separately from the new wheel's successful acceptance.

There were **zero Worker invocations and zero real-provider generation requests**
and no new business task acceptance in this slice. The earlier
[logging evidence](093-local-worker-operational-logs-2026-10-07.md) qualifies its
own frozen runtime; its tests cannot qualify the new digest bytes.

See [change 064](../changes/064-registration-identity-materialization-digest.md)
and [reconstruction guidance](../docs/reference/release-evidence.md).
Attachment links and the broader Kanban/stable gates remain open. No commit,
remote branch update, tag or package publication is implied.
