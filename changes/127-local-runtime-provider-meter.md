# Change 127: Meter provider attempts in the ordinary Runtime profile

## Result

The opt-in source-checkout G0c profile connects the existing process-local G0a meter to the Planner and Researcher `LLMWorker` transports. Both share one epoch, reserve a permit before transport, apply the configured attempt/output-reservation/deadline ceilings, and use zero `LLMWorker` retries. Each new process starts a new epoch; no budget is restored after restart.

The profile adds four typed auxiliary events—permit admitted, transport entered, response observed and attempt finalized—to the existing invocation capture. The writer and strict reader enforce exact closed schemas. They record bounded response metadata and usage presence/state, never credentials, raw usage mappings, bodies, prompts or arbitrary exception text. Local admission or transport entry is not evidence of provider receipt. Unknown usage stays unknown. These events do not become signed facts or task acceptance evidence.

The current configured profile uses an 8,192-token request output cap, timeout up to 60 seconds and bounded HTTP response read of 4 MiB + 1 byte. These are profile limits, not a general provider protocol. The selected credential source is `AIGINEERING_API_KEY`; only its variable name is documented.

Existing `LLMWorker` Candidate construction, action compilation, signed claims, tool continuation, verifier, commitment and recovery owners remain in use. The existing F1a/default profile is unchanged. This slice adds no scheduler, Store table, persistent budget, Redis state, billing claim or provider retry.

## Verification and limits

The exact G0c candidate package passed independent review. Its fake-provider checks include 51 unit, 13 boundary and 13 CLI cases, plus Ruff and format. Five isolated Stores contained 54 tasks and 36 physical fake requests with the expected four-event sequence. Four failure cases completed new recovery tasks through the ordinary claim, Runtime and independent-verifier paths; their original failed task facts remained unchanged. This is fake-transport evidence, not real-provider execution.

Candidate1 and candidate2 review results remain NotPassed for their own snapshots. Candidate3’s independent Passed result is separately bound to its exact source map in the [G0c plan](../docs/plans/runtime-provider-meter.md). No candidate result establishes persistent cross-process budget enforcement, token billing, provider quality or a real-model recovery.

Root’s newer composite full-suite, installed-artifact and build gates passed; independent overall review Passed; final metadata/source-package light closure retains the same724-file code snapshot. This component acceptance does not complete the stage or qualify a stable release.
