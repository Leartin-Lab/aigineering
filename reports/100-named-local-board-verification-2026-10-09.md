# Named local board development verification

Status: bounded development verification; stable qualification remains open.

## Result

Named boards provide persistent local workspace create/list/show/switch/archive
and exact-entry setup recovery. Global `--board` and explicit `-C` route the
existing CLI. The registry is configuration, not task state or authority.
See [change 071](../changes/071-named-local-boards.md),
[ADR-046](../docs/adr/ADR-046-named-board-workspace-routing.md) and
[operations](../docs/reference/task-board.md#named-local-workspaces).

## Snapshot and verification scope

The source version is `0.5.13.dev0`, an uncommitted development worktree over
`01be3f4e585acd7975284a38e88dbdcbb44724c0`. Current runtime has 221 Python
files, with runtime-map SHA-256
`d5c9bc3f845011f9361c7943744b4301ddcb715cd6be5eefe158d72ec0252230`.
The 577-file source/config/test/helper map is
`9ec1b025d44ba25e5fc12c5ae5eb124e50e579d206194aa2fb6a2ebf5a217dea`.

| Check | Actual evidence |
| --- | --- |
| Author focused checks | 21 application/SQLite/architecture tests and 13 native Redis board/domain CLI tests passed. |
| Independent Luna review | Frozen source, boundaries and installed-helper oracles reviewed; separate 21+13 tests passed against isolated stores and Redis domains. |
| Root full regression | 2393 passed, zero skipped, six existing dependency warnings; both application and integration Redis URLs were configured. |
| Source-external package | Fresh virtual environment loaded the installed module outside the checkout; all 221 runtime Python files matched the wheel and source snapshot. |
| Installed metadata workflow | 61 recorded subprocess phases passed their expectations, including seven expected nonzero exits and three controlled fault/recovery pairs inside the installed CLI. No Worker or provider invocation. |

The full-suite source map preceded one helper-only correction to the initial
board-list expected aliases. Runtime and tests remained byte-identical; the
final helper was separately reviewed statically and executed on the installed
wheel. Its SHA-256 is
`61adf7fa722bdc8f71bd742fec9d8c2dbd701a4a2d0e76c07e05e41501226c98`.
The actually installed wheel SHA-256 is
`3a559a1cb5c7ee54c570e5651f93f5cbbfbe9151ff1cbd710096641c7e778121`.

## Installed behavior

The installed run initialized independent managed domains and private keys,
adopted an existing external workspace, and resumed unchanged registrations.
Fresh CLI processes honored saved selection, temporary board selection and
explicit workspace bypass. An in-process installed invocation checked cwd
restoration. Duplicate adoption, conflicting selectors, foreign references,
archived default routing and a replaced Genesis pin returned expected errors.

Two domains contained same-name tasks and output slots. An ordinary signed
human Asset satisfied the output only in its own domain; the other task retained
an unsatisfied output. Exact foreign Asset lookup failed. This is a projection
and domain-isolation control, not Worker execution or independent policy
acceptance. Archive/restore and domain-specific Redis rebuild preserved all 18
SQLite table fingerprints. A disposable Store replacement was restored and
compared with the original fingerprint. Two CLI creation processes launched
before either result was collected preserved both new registry entries.

Controlled injection covered post-Genesis relay pending, registry replacement
failure before publication, and directory fsync failure after visible replacement.
Recovery kept exactly one Genesis and the original private key. Unregistered
entries used ordinary create; visible registrations used explicit `--resume`.
These are injected application/filesystem seams with real retained domains and
normal recovery commands, not real Redis shutdown or physical disk failures.

The successful installed command manifest SHA-256 is
`63112ebe0d13e32c0d61834118ad1db0b895496cc6441c2a14632777e8b61b19`;
its summary SHA-256 is
`920d5b173e4659ab39e1114230b39c636df8716a384d94680915c437b0b4ca38`.
Original phase streams, embedded scripts, hashes and exit statuses are retained
privately, alongside failed attempts.

## Retained failures and limits

Independent review caught managed-path symlink escape and a nonexecutable
post-replacement retry suggestion; source fixes and behavior tests preceded
the final full run. Earlier author checks included an unsupported test option
and an unsuccessful sandbox Redis connection. The unsupported-option run has
tool-transcript evidence only; it was not reconstructed as an original raw file.

The first installed run stopped because the checker omitted an already-adopted
third alias from its expected list. The actual CLI list was correct. Its streams
remain separate from the corrected helper's successful fresh-fixture run.

Windows behavior, real infrastructure/disk faults, long-running Workers during
archive and OS/multi-user isolation were not verified here. No cross-domain
business acceptance, current real-provider usefulness, complete Kanban parity
or stable-release qualification is inferred. No commit, push, tag or package
publication was performed.
