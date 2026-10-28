from __future__ import annotations

import inspect

from aigineering import runtime


def test_scan_order_hook_is_runtime_only_and_optional():
    source = inspect.getsource(runtime)
    signature = inspect.signature(runtime.claim_next_package)
    parameter = signature.parameters["order_contract_ids"]

    assert parameter.default is None
    assert "priority" not in source.lower()
    assert "aigineering.plugins.task_priority" not in source
    assert "_reorder_routed_contracts" in source
