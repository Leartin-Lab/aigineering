# Change 097: Allow policy review of eligible retry tasks

An ordinary retry task with a frozen independent-acceptance policy can request
the existing exact-output review flow. The shared review-source predicate keeps
method/plugin labels and system, recovery and unknown origins excluded. A newly
created reviewer child for a retry source uses human origin so retry lineage does
not mistake review work for a retry publication. Existing human/plan reviewer
origins and exact request replay remain unchanged.

The retry source keeps its immutable identity and origin. Review still requires
the existing current authorization, exact claimed output and producer, policy,
allowance and writer checks. A `changes` verdict does not qualify the output;
retry-source continuation through revision remains unsupported.

Validation: signed application and plugin request regressions, full retry-review
acceptance and lineage checks, existing review suite, architecture, Ruff and
formatting checks.
