# Persistent local Fleet service

`aig fleet run` remains a bounded run for one selected task. Use the service commands when a local Fleet should keep pulling eligible work from the selected workspace's fact domain:

```bash
aig -C workspaces/research fleet start --config workers.toml --json
aig -C workspaces/research fleet status --json
aig -C workspaces/research fleet stop --wait-timeout 30 --json
aig -C workspaces/research fleet restart --config workers.toml --wait-timeout 30 --json
```

The Fleet config must resolve to the selected workspace's normal `.aig/store.db`. The persistent service rejects a custom `db_path` and `:memory:`; existing bounded/configured runs keep their existing custom-store behavior. Start requires an initialized domain and available Redis. It validates the config and Worker adapters, pins the exact Genesis and config digest, reserves a private process generation, then uses the normal signed local Worker registration owner. No task or root Contract is created by service setup.

For configured LLM workers, start also checks that the configured environment variable or existing LLM fallback environment variables contain a non-empty credential. Credentials are never persisted in service metadata. Local and tool workers do not require a provider credential.

Before launching the service process, start constructs adapters through the same application resource-preparation owner used by configured runs. The child reconstructs process-local adapters through that owner. A local `worker_factory` keeps the existing strict `module:callable` form and is invoked during both preflight and child construction; no path-based factory loading is added. Start rejects malformed or unsafe prior state metadata. It may replace a valid stale generation only after acquiring the exclusive lock and validating its workspace and generation fields. After launch, only the child updates generation state, preserving a fast startup failure.

The service uses the same signed claim/renew/submit path and per-capacity-slot SQLite connections as the existing local Fleet. Redis supplies required record relay, discovery and wakeups; SQLite remains authoritative. Idle service loops own no task queue or synthetic progress, though canonical maintenance can write its ordinary expiry, completion or recovery facts.

Status fields are process observations, not task state. A private lock and generation nonce identify the one process allowed to act on a service; a PID is diagnostic only. Config changes are reported as drift. Restart does not hot-reload or rebind a live Worker; it waits for the previous generation to drain and validates the supplied configuration again.

Within the child process, concurrent lifecycle callbacks serialize their complete metadata read/merge/write and preserve forward-only state transitions. A late `ready` update cannot reopen a generation after `stopping` is recorded. Once the generation records `stopped`, `failed`, or the existing `degraded` state, the complete metadata snapshot is frozen; late callbacks cannot replace its `active_slots` or `error_code`. The successful `stopped` snapshot is written only after the persistent slot runner has joined, the stop watcher has exited, and the maintenance Store has closed. This process-local lock coordinates the child’s own writers; it is not a cross-process task or claim fence.

Stop closes admission to new claim-pull cycles and waits for already admitted work to drain. A pull admitted before the gate closes may still commit a claim and invoke while stop is in progress. Stop never cancels a task, clears assignment, resets a claim or kills an unrelated process. If the wait expires, the command returns `stopping` to report that the request was not acknowledged before the deadline; metadata can still show `ready` until the child observes the request. Once the child records `stopping`, later slot updates may refresh `active_slots` but cannot restore `ready`. Do not start another generation until the old lock is released. A crash can leave an active claim until ordinary lease maintenance. External side effects may be unknown, and exactly-once execution is not promised.

`status` and `stop` use only local process metadata and can be used while Redis is unavailable. Start and dispatch remain blocked until Redis is healthy and accepted signed setup has synchronized. Process metadata contains no credentials, prompts or task state. POSIX file locks/no-follow access are required; Windows and multi-user service control are not qualified.

Bounded `fleet run` and configured `run --config` keep their existing
`worker_errors` and `capture_warnings` fields and add `worker_failure_details`.
For an invocation failure, the detail includes task, claim, package, Worker and
epoch only when the shared read-only diagnostics owner proves all of them for
the same claim at one Store revision. It includes only a recognized failure
category, retryability, status code and allowlisted numeric usage. If that proof
is missing or inconsistent, the detail is `diagnostic_unavailable` without
claim identifiers. An unexpected slot exception is a fixed unbound `slot_error`;
its exception text is never returned. See
[task attempt inspection](task-board.md#inspect-claim-attempts) and
[change 078](../../changes/078-fleet-failure-diagnostics.md).
