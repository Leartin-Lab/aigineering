# Pin configured root input Assets

Status: Implemented development increment
Date: 2026-10-23

When a new configured run declares root input roles, the application binds each
role to the single Asset ID included in that run's committed resource
publication. This also applies when the preset omits `sensitive_input_policy`;
the application creates the guard policy for those inputs. Disclosure and
verification then select each exact Asset through the shared
`input_asset_bindings` owner, so a historical or later same-name Asset cannot
join the configured root's input set. Other sensitive policy fields and unused
historical bindings are preserved.

An active explicit binding must already match the configured Asset. A conflict,
a missing prepared Asset, or a missing or ambiguous published Asset fails with
an observable error; configured bindings are never silently replaced. Binding
conflicts are checked against committed Asset IDs before root declaration.
Configured runs with no input roles and no sensitive input policy keep their
existing Contract identity.

Resume selects the already committed Contract and does not recompute bindings
from the current configuration or files. Planner-created children continue to
inherit the frozen policy through existing planning semantics. This pins local
Asset selection only; it does not grant capabilities or establish independent
acceptance.
