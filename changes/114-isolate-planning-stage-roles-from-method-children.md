# Isolate planning stage roles from method children

Status: Implemented development increment
Date: 2026-10-23

Planning stage labels identify the Worker role that compiles a draft,
dependency analysis, or blueprint. New claim-bound method children created for
`/tool`, `/fail`, or a parallel tool item have their own execution role; they no
longer inherit those six stage-control labels. Ordinary labels and
action-specific plugin labels are preserved. Tool continuations and
same-obligation recovery retain their stage role, so a compile Contract can use
an allowed tool and later compile its blueprint.

Prompt construction and Worker compilation use one read-only active-stage
resolver. System, recovery, or retry method Contracts with matching typed method
metadata and its `plugin:*` or legacy `method:*` label take precedence over
inherited stage labels. Ambiguous legacy
compile-labelled recovery keeps the blueprint compiler fence active, including
when old description metadata cannot identify one stage. Malformed metadata does
not bypass that fence.

New nested staged plans retain only the current stage label. Historical
method-publication recognition separately reconstructs both the former
label-inheriting shape and the current shape for serial and parallel tool
methods. This changes no old Contract, publication, authority, or allowance;
history matching remains read-only and exact.

Signed Worker behavior tests cover compile → tool → committed observation →
compile continuation → blueprint publication, `/fail`, parallel tool items,
legacy mixed-label method and recovery roles, malformed stage metadata, and
nested-stage label cleanup. Architecture checks bind prompt/compiler routing to
the shared role owner and retain both current and legacy recovery proof paths.
