"""Installed signed control fixtures for injected effect declarations.

This verifies trusted host configuration, not model execution or a task output.
"""

from __future__ import annotations

import json
from pathlib import Path

from installed_smoke import run


_SCRIPT = r"""
import json, sys
from pathlib import Path
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.domain import initialize_genesis
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.effect_projectors import EffectProjection
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import ActorKey, CandidateEffect, create_genesis_manifest
from aigineering.protocol.runtime_record import create_runtime_record

directory = Path(sys.argv[1])
directory.mkdir(mode=0o700, exist_ok=False)
exclusive_type = "fixture.exclusive.changed"

def projector(record_type):
    def emit(effect, candidate, receipt_id, context):
        return EffectProjection(
            (create_runtime_record(
                record_type, {"candidate_id": candidate.id},
                causal_parents=(receipt_id,),
            ),), relation_target=candidate.id,
        )
    return emit

def strict(record_type, allowed, reserved=frozenset()):
    return EffectHandler(
        "fixture.emit", projector(record_type),
        direct_output_fact_types=frozenset(allowed),
        reserved_fact_types=frozenset(reserved),
    )

def malformed(records):
    def emit(effect, candidate, receipt_id, context):
        return EffectProjection(records, relation_target=candidate.id)
    return EffectHandler(
        "fixture.emit", emit,
        direct_output_fact_types=frozenset({"fixture.allowed"}),
    )

cases = [
    ("strict-allowed", "fixture.emit", {"fixture.emit": strict("fixture.allowed", {"fixture.allowed"})}, True),
    ("legacy-typed", "fixture.emit", {"fixture.emit": EffectHandler("fixture.emit", projector("fixture.unexpected"), emitted_fact_types=frozenset({"fixture.allowed"}))}, True),
    ("legacy-tuple", "fixture.emit", {"fixture.emit": ("fixture.emit", projector("fixture.unexpected"))}, True),
    ("strict-empty", "fixture.emit", {"fixture.emit": strict("fixture.unexpected", set())}, False),
    ("strict-undeclared", "fixture.emit", {"fixture.emit": strict("fixture.unexpected", {"fixture.allowed"})}, False),
    ("malformed-null-member", "fixture.emit", {"fixture.emit": malformed((None,))}, False),
    ("malformed-object-member", "fixture.emit", {"fixture.emit": malformed(({},))}, False),
    ("malformed-list-container", "fixture.emit", {"fixture.emit": malformed([])}, False),
]
owner = strict(exclusive_type, {exclusive_type}, {exclusive_type})
cases.extend([
    ("reserved-owner", "fixture.owner", {"fixture.owner": owner}, True),
    ("reserved-other-strict", "fixture.other", {"fixture.owner": owner, "fixture.other": strict(exclusive_type, {exclusive_type})}, False),
    ("reserved-other-legacy", "fixture.other", {"fixture.owner": owner, "fixture.other": ("fixture.emit", projector(exclusive_type))}, False),
])
results = {}
for name, effect_type, handlers, expected in cases:
    store = SQLiteStore(directory / (name + ".sqlite"))
    try:
        signer = Ed25519Signer()
        key = ActorKey("fixture:operator", "fixture-key", signer.kind, signer.signer_id, ("fixture.emit",))
        genesis = create_genesis_manifest("installed-ownership-" + name, (key,), "fixture-policy")
        initialize_genesis(store, genesis)
        publisher = CandidatePublisher(store, store, genesis, key, signer, additional_effects=handlers)
        decision = publisher.publish((CandidateEffect(effect_type, {}),), idempotency_key=name)
        assert decision.accepted is expected, name
        direct_records = [record for _, record in store.scan_runtime_records() if record.record_type.startswith("fixture.")]
        assert len(direct_records) == int(expected), name
        if not expected:
            assert any(record.record_type == "candidate.rejected" for _, record in store.scan_runtime_records()), name
            assert not any(record.record_type == "candidate.committed" for _, record in store.scan_runtime_records()), name
        before = [record.id for _, record in store.scan_runtime_records()]
        store.rebuild_runtime_materializations()
        assert before == [record.id for _, record in store.scan_runtime_records()], name
        results[name] = {"accepted": expected, "direct_fact_count": len(direct_records), "rebuild_records_equal": True}
    finally:
        store.close()

duplicate_rejected = False
from aigineering.core.effect_projection import freeze_additional_effects
try:
    freeze_additional_effects({"fixture.owner": owner, "fixture.second": owner})
except ValueError:
    duplicate_rejected = True
assert duplicate_rejected
print(json.dumps({
    "classification": "installed_signed_generic_control_fixture",
    "cases": results,
    "duplicate_exclusive_owner_rejected": duplicate_rejected,
    "provider_requests": 0,
    "business_outputs": 0,
}, sort_keys=True))
"""


def installed_effect_ownership_smoke(
    python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Check installed library declarations through signed SQLite publication."""
    return json.loads(
        run(
            [str(python), "-c", _SCRIPT, str(cwd / "injected-effect-ownership")],
            cwd=cwd,
            env=env,
        )
    )
