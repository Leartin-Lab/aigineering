# v0.5.3 boundary convergence acceptance

Date: 2026-08-09; harness stabilization updated 2026-08-10
Scope: single-machine SQLite reference runtime

## Result

v0.5.3 closes several cases where the implemented kernel and its intended
ownership had drifted while preserving the v0.5 protocol model. It does not
claim public-network hardening, an external security audit, or deterministic
compliance from any particular model provider.

## Accepted changes

- terminal facts fence and release unrelated active claims in the same SQLite
  transaction, and deterministic commitment conflicts leave rejection facts;
- ordinary Worker `/exec` output is one signed atomic content, definition, and
  assertion graph batch with a compatibility Asset projection;
- causal allowance facts are the sole runtime budget source;
- terminal RuntimeRecord construction has one owner;
- planning scaffold semantics are Plugin-owned;
- planning rejects label scope widening before commitment;
- the public documentation map names one owner for each kind of truth.

The bounded transitions and their tests are recorded in Changes 005 through
012. Existing ADR-013 and ADR-017 remain the durable decisions for causal
allowance and the signed definition/content graph; this maintenance release did
not add a competing architecture decision.

## Deterministic gates

The versioned release tree passed:

- `ruff check src/aigineering tests`;
- `ruff format --check src/aigineering tests` across 221 files;
- `pytest -q`: 1147 passed, 3 skipped;
- the focused crash, concurrent Worker, reconstruction, WorkerHost, and claim
  suite: 50 passed;
- wheel and sdist build for 0.5.3 with `twine check` passing both artifacts.

The dev dependency pins Ruff 0.15.17 so local and clean Linux CI evaluate the
same release gate instead of inheriting changing default rule sets from an
unbounded future Ruff release.

A clean virtual environment installed the wheel, reported version `0.5.3`,
initialized a new domain, completed a claim-bound mock Worker task, and used
fresh CLI processes to reopen the database and read the signed graph-backed
output and Contract. The wheel also imports the public
`HarnessCandidateAdapter`; the sdist contains the public design, Skill,
documentation map, Change 012, harness migration guide, and this report. It
excludes tests and private workspaces. Both repository Skills passed their
validator.

The unchanged recursive execution surface remains covered by the deterministic
staged-planning, nested-replanning, restart, and long-chain suites. Historical
bounded 20-step and nested model evidence is retained in the
[v0.5.0 boundary report](050-post-review-boundary-hardening-2026-07-19.md);
v0.5.3 does not relabel that historical run as new evidence.

## Bounded real-LLM evidence

Provider/model: DeepSeek OpenAI-compatible API / `deepseek-v4-flash`.
Credentials remained in the ignored local environment and did not enter the
Store, trace, report, or artifact.

One clean ordinary task completed through the default LLM Worker graph path
without an explicit `--worker` or model argument. Its
single Candidate committed `asset.content.publish`,
`asset.definition.publish`, and `asset.assert`; a fresh CLI process rebuilt the
completed task, exact output, token usage, zero rejections, zero recovery, and
zero silent-failure risks. The accepted assertion was signed by the Worker key.
The run consumed 678 prompt and 65 completion tokens.

DeepSeek also returned one planning blueprint as a nested JSON value where the
protocol requires Asset content text. The LLM adapter now canonicalizes that
provider presentation and removes only a complete leading reasoning wrapper
before strict action parsing. A synthetic provider run then compiled the
normalized blueprint into two ordinary Contract declarations. Candidate,
authority, and commitment validation remain unchanged.

A planning run reached compile and exposed an invented child label. Commitment
rejected it fail-closed. The planning Plugin and compiler prompt were aligned
with the existing parent-label fence, then covered by deterministic tests.
Later bounded provider attempts included explicit `candidate_encoding_failure`
and `invalid_action` terminal outcomes. Their claims were durably released and
their task projections exposed the failure without hanging. They are negative
provider-format evidence, not presented as a successful live planning chain.

The supported claim is therefore precise: the runtime accepts conforming
Worker effects, reconstructs accepted graph output, and makes non-conforming
or late work observable. It does not guarantee that a stochastic model always
emits the required action schema.

## Release limits

- SQLite remains the authoritative single-machine Store.
- Redis remains a disposable read projection.
- Cross-machine consensus and hostile-network authentication remain outside
  this release.
- Semantic correctness still requires independent acceptance where policy
  demands it.
