# Terminal allowance validation

`allowance.extinguished` is now accepted only as the unique positive-integer
remainder for a Contract, paired with its unique canonical `lifecycle.terminal`
by exact causal parent and terminal reason. Exact record replay remains
idempotent; unrelated commits do not retroactively rewrite historical facts.
SQLite's second allowance proof now includes the Candidate's main pending facts
and the derived terminal consequences, preserving same-transaction
reservations when another writer completes a Contract during projection.

SQLite Candidate ingress, Worker invocation failure, and claim expiration now
use the same terminal-allowance materializer. Claim status, terminal outcome,
and any remaining allowance are committed together; a new child reservation
cannot draw from an already terminal source. This applies to new writes only:
exact replay and reconstruction do not repair historical missing burns.

The existing terminal-event values have one dependency-neutral owner while
`runtime_projection.TERMINAL_EVENTS` remains available to existing readers.
No new protocol field, fact type, terminal authorization path, or schema was
added. Updated writers enforce the new check; stop older Worker and writer
processes before relying on it. Existing history is not rewritten.

Validation: focused allowance, paired-return, Worker command, architecture, Ruff,
formatting, and diff checks passed.
