# Change 124: Add an opt-in metered direct provider adapter

## Problem

The direct Harness was a strict deterministic offline fixture. It could not observe real provider usage or enforce a shared bounded request allowance, so it could not supply a real-provider comparison arm.

## Resulting behavior

A separate `direct-live.py` command uses explicit `deepseek-chat-v1` parsing and bounded standard-library HTTPS. Default offline wire and `direct-run.py` remain unchanged. One process RunMeter admits every physical attempt with conservative output reservation and cooperative deadline; permit admission, transport entry and response are distinct observations. Complete bounded JSON usage is passed unchanged, including partial/invalid components and HTTP failures. Unknown usage remains unknown. Only complete 500/503 responses can use configured bounded retry; redirects, partial bodies, rate limits and unknown failures do not silently retry.

The CLI validates nonsecret inputs before exclusive evidence initialization and named environment key lookup. Safe events omit prompts, raw responses, tool arguments, reasoning and untrusted tool IDs; exact IDs/reasoning remain in memory for continuation. Only an oracle-accepted business report is exported. Configuration and artifacts never take a raw key argument. Examples ship in the sdist, not the wheel. No dependency, Runtime, task, claim or commitment owner changes.

## Verification and limits

Transport fixtures cover bounded reads, EOF/error handling and safe diagnostic
output. They do not establish real-provider behavior, durable restart budgets,
input-token or money caps, fair comparisons or quality/cost advantages.
