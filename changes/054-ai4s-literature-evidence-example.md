# AI4S literature evidence example

The public examples now include both an installable `literature-evidence`
skill and an executable audited run. The run uses an explicitly configured
OpenAlex ToolRegistry, an LLM Worker, a separate ToolWorker, signed Candidate
commitment, and a distinct verifier actor.

The verifier accepts a report only when its JSON shape is valid and every
citation ID occurs in a successful OpenAlex observation produced within the
task's descendant graph. The report task remains incomplete until that exact
descendant Asset is independently attested. Fixture replay and live OpenAlex
use the same adapter; scientific semantics remain outside the runtime kernel.
