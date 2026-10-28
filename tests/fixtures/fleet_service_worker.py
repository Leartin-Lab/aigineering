"""Provider-independent local Worker used by persistent Fleet tests."""

import json

from aigineering.protocol.types import Candidate


class ServiceWorker:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def invoke(self, contract, disclosed_assets):
        del disclosed_assets
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps(
                {
                    "outputs": {
                        name: f"service:{contract.id}:{name}"
                        for name in contract.outputs
                    }
                }
            ),
        )


def build_worker(*, worker_id: str) -> ServiceWorker:
    return ServiceWorker(worker_id)


class BarrierServiceWorker(ServiceWorker):
    def __init__(self, worker_id: str, started_path: str, release_path: str) -> None:
        super().__init__(worker_id)
        self.started_path = started_path
        self.release_path = release_path

    def invoke(self, contract, disclosed_assets):
        import time
        from pathlib import Path

        Path(self.started_path).write_text("started", encoding="utf-8")
        deadline = time.monotonic() + 20
        while not Path(self.release_path).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("test invocation barrier expired")
            time.sleep(0.01)
        return super().invoke(contract, disclosed_assets)


def build_barrier_worker(*, worker_id: str) -> BarrierServiceWorker:
    import os

    return BarrierServiceWorker(
        worker_id,
        os.environ["AIG_FLEET_TEST_STARTED_FILE"],
        os.environ["AIG_FLEET_TEST_RELEASE_FILE"],
    )
