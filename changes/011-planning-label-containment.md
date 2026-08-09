# Change 011: Planning label containment

Status: Implemented and verified
Target: v0.5.3

## Problem

A planning compiler could accept a child label outside its parent scope and
leave the later commitment boundary to reject the whole Candidate. The core
remained fail-closed, but the Plugin and model prompt did not express the same
containment rule, turning a recoverable planning error into a terminal Worker
submission failure.

## Intended change

- planning rejects child labels outside the parent's non-Plugin label set;
- compiler prompts state the exact allowed label set and forbid invented or
  copied `plugin:*` labels;
- valid label subsets retain their context-selection meaning;
- commitment keeps its independent label fence.

## Non-goals

- granting authority through labels;
- resolving labels again during replay;
- allowing planning stages to leak their control labels into business tasks;
- weakening exact context Asset binding.

## Verification

Architecture and prompt tests cover invented labels, mixed supersets, valid
subsets, stable compile rejection fields, and the independent commitment fence.
A bounded real-LLM planning run reproduced the original failure before the
change. Post-change provider runs either obeyed the boundary or ended with an
explicit terminal Worker-format failure; no widened label or silent task end
was accepted.
