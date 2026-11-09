from __future__ import annotations

from dataclasses import replace

import pytest

from aigineering.agent.mock import MockWorker
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import (
    ActorKey,
    create_genesis_manifest,
)
from aigineering.protocol.types import Contract
from aigineering.runtime import claim_next_package


def _environment(tmp_path, *, worker_id="scan-worker"):
    store = SQLiteStore(tmp_path / "claim-scan-order.sqlite")
    signer = Ed25519Signer()
    key = ActorKey(
        worker_id,
        f"key:{worker_id}",
        signer.kind,
        signer.signer_id,
        ("worker.submit",),
    )
    genesis = create_genesis_manifest(
        "claim-scan-order-test", (key,), "policy:scan-order"
    )
    initialize_genesis(store, genesis)
    store.register_worker(
        WorkerRegistration(
            worker_id,
            version="1",
            actor_id=key.actor_id,
            key_id=key.key_id,
        )
    )
    worker = MockWorker(worker_id=worker_id)
    from aigineering.agent.worker import WorkerHost

    host = WorkerHost(worker, genesis, key, signer, registration_version="1")
    return store, None, host


def _add_tasks(store, *contracts):
    for contract in contracts:
        store.add_contract(contract)


def _claim(store, host, *, contract_id=None, order_contract_ids=None):
    command = host.claim_candidate(
        request_id=f"claim:{contract_id or 'scan'}:{id(order_contract_ids)}",
        contract_id=contract_id,
    )
    return claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract_id,
        expected_registration_version=host.registration_version,
        claim_candidate=command,
        order_contract_ids=order_contract_ids,
    )


def test_order_hook_reorders_actual_signed_sqlite_claims(tmp_path):
    store, _publisher, host = _environment(tmp_path)
    _add_tasks(
        store,
        Contract(id="task:a", outputs=("a",), budget=1),
        Contract(id="task:b", outputs=("b",), budget=1),
    )
    observed = []

    def higher_first(contract_ids):
        observed.append(contract_ids)
        return ("task:b", "task:a")

    claimed = _claim(store, host, order_contract_ids=higher_first)
    assert observed == [("task:a", "task:b")]
    assert claimed is not None
    assert claimed.contract.id == "task:b"
    assert store.get_claim("task:b") is not None
    assert store.get_claim("task:a") is None
    store.close()


@pytest.mark.parametrize(
    "orderer",
    [
        lambda ids: ids[:-1],
        lambda ids: (*ids, "task:foreign"),
        lambda ids: (ids[0], ids[0]),
        lambda ids: [*ids],
        lambda ids: (ids[0], 7),
    ],
)
def test_invalid_hook_output_rejects_before_any_claim(tmp_path, orderer):
    store, _publisher, host = _environment(tmp_path)
    _add_tasks(
        store,
        Contract(id="task:a", outputs=("a",), budget=1),
        Contract(id="task:b", outputs=("b",), budget=1),
    )

    with pytest.raises(ValueError, match="claim order hook"):
        _claim(store, host, order_contract_ids=orderer)

    assert store.get_claim("task:a") is None
    assert store.get_claim("task:b") is None
    store.close()


def test_non_callable_order_hook_rejects_without_claim(tmp_path):
    store, _publisher, host = _environment(tmp_path)
    store.add_contract(Contract(id="task:one", outputs=("one",), budget=1))

    with pytest.raises(ValueError, match="claim order hook must be callable"):
        _claim(store, host, order_contract_ids=7)  # type: ignore[arg-type]

    assert store.get_claim("task:one") is None
    store.close()


def test_readiness_still_skips_first_ordered_but_blocked_task(tmp_path):
    store, _publisher, host = _environment(tmp_path)
    _add_tasks(
        store,
        Contract(
            id="task:blocked",
            outputs=("blocked",),
            budget=1,
            activation="missing_input",
        ),
        Contract(id="task:ready", outputs=("ready",), budget=1),
    )

    claimed = _claim(
        store,
        host,
        order_contract_ids=lambda ids: ("task:blocked", "task:ready"),
    )
    assert claimed is not None
    assert claimed.contract.id == "task:ready"
    assert store.get_claim("task:blocked") is None
    store.close()


def test_hook_only_sees_worker_routed_contracts(tmp_path):
    store, _publisher, host = _environment(tmp_path)
    _add_tasks(
        store,
        Contract(
            id="task:vision",
            outputs=("vision",),
            budget=1,
            worker_capabilities=("vision",),
        ),
        Contract(id="task:general", outputs=("general",), budget=1),
    )
    observed = []

    def reverse_routed(ids):
        observed.append(ids)
        return tuple(reversed(ids))

    claimed = _claim(store, host, order_contract_ids=reverse_routed)
    assert observed == [("task:general",)]
    assert claimed is not None
    assert claimed.contract.id == "task:general"
    assert store.get_claim("task:vision") is None
    store.close()


def test_explicit_target_bypasses_order_hook(tmp_path):
    store, _publisher, host = _environment(tmp_path)
    _add_tasks(
        store,
        Contract(id="task:a", outputs=("a",), budget=1),
        Contract(id="task:b", outputs=("b",), budget=1),
    )

    def must_not_run(_ids):
        raise AssertionError("explicit contract target must bypass scan ordering")

    claimed = _claim(
        store,
        host,
        contract_id="task:a",
        order_contract_ids=must_not_run,
    )
    assert claimed is not None
    assert claimed.contract.id == "task:a"
    store.close()


def test_writer_registration_fence_catches_change_after_signed_preflight(tmp_path):
    store, _publisher, host = _environment(tmp_path)
    contract = Contract(id="task:registration-fence", outputs=("report",), budget=1)
    store.add_contract(contract)
    current = store.get_worker_registration(host.worker_id)
    assert current is not None

    def rotate_registration(contract_ids):
        store.register_worker(replace(current, version="2", capacity=2))
        return contract_ids

    with pytest.raises(
        ValueError, match="registration changed after package selection"
    ):
        _claim(store, host, order_contract_ids=rotate_registration)
    assert store.get_claim(contract.id) is None
    store.close()
