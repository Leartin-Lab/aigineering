"""Source-compatibility exports for plugin-native task semantics."""

from aigineering.plugins.task_semantics import (
    continuation_contract,
    contracts_from_plan_asset,
    method_context_content,
    method_contract,
    method_payload,
    retry_contract,
    system_asset,
)

__all__ = (
    "continuation_contract",
    "contracts_from_plan_asset",
    "method_context_content",
    "method_contract",
    "method_payload",
    "retry_contract",
    "system_asset",
)
