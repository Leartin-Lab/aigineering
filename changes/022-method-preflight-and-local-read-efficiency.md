# Method preflight and local read efficiency

Status: implemented in the v0.5.11 development line.

Method test preparation compiles the complete plan against uncommitted input
proposals before publishing inputs. Invalid names, budgets, tool grants and known
held-out context therefore fail before input publication. Inputs are published
as one batch, and the final Contracts bind the returned durable Asset IDs.

Input publication, Contract publication and run-manifest publication remain
separate signed operations. This is not an all-or-nothing transaction across the
whole method command: interruption or a later commitment rejection can leave
earlier accepted facts. The commitment kernel and its authority checks are
unchanged.

Worker package selection filters routing requirements before constructing task
views, uses exact Contract lookup when requested, and shares a frozen set of
available names within one selection pass. Output satisfaction, disclosure and
transactional claim checks retain their existing paths. No cross-pass cache is
introduced, and historical projections continue to reconstruct their own facts.

Method evaluation indexes outputs by name and caches ancestry within one
evaluation call. Exact case lineage, ambiguous-output rejection and independent
qualification remain unchanged. These optimizations reduce repeated reads and
traversal; they do not introduce incremental history processing or a new Store.
