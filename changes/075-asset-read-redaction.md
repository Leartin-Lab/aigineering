# Asset read redaction

## Problem

The operator CLI and HTTP Asset serializers returned stored text directly even
when the canonical disclosure view marked an Asset `redacted` or `transformed`.
That made administrative inspection expose a body that Worker disclosure
already replaced with the redaction marker.

## Resulting design

CLI `asset show` and every HTTP response built by `_asset_response` now reuse
`redact_for_disclosure`. Non-original views return `[redacted]` while preserving
stored identity and hashes, and responses include `disclosure_view`. An original
Asset remains visible to operator inspection even when `promptable` is false.
Derived slices retain both the source disclosure view and promptability, so a
redacted, transformed, or operator-only body does not become an ordinary
Worker-disclosable Asset through slicing.
Stored source Assets and their signatures are unchanged. A newly created slice
keeps its derived content identity and signature while inheriting the source's
disclosure-view and promptability metadata.

## Public compatibility

Name and exact-ID selection retain their existing behavior. Original Assets
retain their existing visible body. HTTP creation, candidate, and slice
responses use the same serializer as reads.

## Required verification

Signed synthetic fixtures cover redacted and transformed bodies, original and
nonpromptable-original compatibility, CLI name/ID text/JSON, HTTP Asset GET
families, publication and Candidate responses, and unchanged Store facts and
revision around reads. An architecture constraint keeps CLI and HTTP on the
canonical disclosure helper. Signed slice fixtures also verify that source
classification is preserved and Worker disclosure still omits nonpromptable
Assets.

## Integrated verification

The integrated check passed Ruff, formatting, package build, the full test suite
(2,457 passed with 6 existing warnings), and a fresh external-install read
verification. Scoped signed HTTP and CLI checks also confirmed that redacted
bodies stay hidden and reads leave Store state unchanged. Slice checks verify
source disclosure metadata propagation; they do not add guarantees about every
range-conversion form.

## Closure

This change closes the Asset body exposure through these CLI and HTTP Asset
serializers. It does not claim coverage of unrelated descriptor, task result,
attachment export, or task context interfaces.
