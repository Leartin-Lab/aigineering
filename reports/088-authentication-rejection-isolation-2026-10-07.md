# Authentication rejection isolation development verification

Date: 2026-10-07
Version: 0.5.13.dev0
Status: Bounded development verification complete; stable qualification remains open.

## Confirmed defect and resulting behavior

An isolated reproduction with fresh keys, a temporary SQLite store and an
original signed operational claim found that an invalid-signature Candidate
could consume the matching active claim and create an unparented failed attempt,
terminal outcome and allowance extinguishment. It had no admitted receipt.
The original reproduction and failed runs are retained. No external target,
provider or Worker invocation was involved.

Authentication failures now enter the existing audit owner before claim-bound
attempt, replay or writer paths. They persist only observable rejection/trace;
claimed IDs and binding metadata cannot mutate a claim, close an obligation,
consume allowance or schedule recovery. Claimed-ID collision with an accepted
Candidate cannot suppress the rejection. Repetition is audit-idempotent.
Authenticated but inadmissible output retains exact receipt-backed failed-attempt
and ordinary recovery semantics. Both authenticated writer rejection paths share
exact matching receipt selection through the existing owner.

The Luna author supplied fixture checks and a remediation addendum. A separate
Luna independently reviewed the final delta and passed 63 focused authentication,
claim, CLI missing-key, HTTP, writer-conflict and governance tests. Root verified
those exact file hashes equal the integrated source below. Earlier overlapping
14-test evidence is retained separately. CLI and HTTP fixture tests substitute
Redis preflight/relay seams; they are not deployment verification.

## Exact source and integrated verification

The uncommitted working tree is based on HEAD
`01be3f4e585acd7975284a38e88dbdcbb44724c0`. The complete source/config/test/
script/vector inventory contains 530 files, SHA-256
`87e21254d4a866255177feda418062cc81511828067cf4463199640ac262c31e`.
The runtime inventory contains 208 Python files, SHA-256
`9df6c6892280ce7f9573d768a4eaf5f6029527c8bc22a005cb01ee0b53bef70b`.
The maps match before and after the integrated run. Public documentation is
checked separately from this source evidence.

The serial integrated suite passed **2128 tests**, with 6 dependency deprecation
warnings, in 109.45 seconds. Ruff check and format passed for source/tests;
456 files were already formatted. All 13 Node vector consumer invocations
passed; their fixture and consumer hashes are unchanged across the two source
snapshots. Author and independent subsets overlap this suite and are not additive.
The integrated stdout SHA-256 is
`a45e8164b79ebae42918450befdf6435d8925ba7a56c147bf77ed7b2468d0b92`.

The first integrated run retained 2 failures and 2126 passes: the commitment
coordinator exceeded its existing size constraint, and an old missing-key test
expected an unauthenticated submission to consume a claim. Delegation to the
existing rejection owner restored the 299-line coordinator without relaxing the
gate, and the obsolete oracle was corrected. The second run above validates
the resulting source; the first failure logs remain retained.

## Artifact acceptance

The first wheel and sdist built successfully and passed Twine metadata checks.
Their complete 208-file runtime inventory matches the tested source; all 172
then-current public Markdown files match the sdist and archive privacy checks
pass. The root guide's 68 direct references resolve. Final delivery documents
are checked separately from that first archive.

The original probe failed after the invalid-auth case at its second contrast
claim. Its fresh Worker had the default capacity of one: the first claim now
correctly remains active and occupies that slot. The original probe and failed
logs remain unchanged. A private adapted copy registers two legitimate fixture
slots, retaining the first active claim and the separate contrast task. Added
import checks require the installed package outside the source checkout. Root
reviewed this fixture delta; no production or test source was changed.

The adapted probe passed against the same offline-installed first wheel. The
invalid-auth case stays active and adds only authentication rejection and trace,
with no lifecycle/allowance/recovery. The valid signed projection rejection
closes its authenticated attempt and schedules ordinary recovery. These are
fixture-backed signed SQLite checks, with no Redis, HTTP, Worker invocation or
provider request. The wheel SHA-256 is
`94223e1ae6b51eb70adb2d448ff40fa5ee8a92b49a336211e786efe38453af79`;
the adapted probe SHA-256 is
`e31dd79329915e9969614b8cd70529e2c99024d08570600a8c6c8c766842b8f7`.
The result manifest SHA-256 is
`ef3f4047e8b6b73933f26ed373633f2b997cd19acd2e6f5332bfb5195daf85f4`.
An initial adapted run's missing import failure is retained separately.

## Limits

This adds no schema, fact type, key registration policy or scheduler. Historical
incorrect facts remain immutable; audit/rebuild does not repair them or reset
claims. Current code cannot constrain already-running older writers; stop old
Workers before switching binaries. Any unfinished old obligation needs explicit
signed recovery or new work.

Deterministic tests and the isolated probe do not establish hostile-network
hardening, process isolation or model quality. Provider calls and business
outputs in this slice are zero. Current-provider actual output and wider stable
qualification remain open. No commit, push, tag or publication occurred. See
[the change](../changes/057-authentication-rejection-isolation.md) and
[the invariant](../docs/boundary-invariants.md).
