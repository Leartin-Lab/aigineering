"""Continuation task publication as a pure plugin."""

from __future__ import annotations

from aigineering.core.methods import continuation_contract, method_payload
from aigineering.plugins.base import PluginProposal, PluginRequest
from aigineering.protocol.effect_builders import contract_declaration_effect


class ContinuationTaskPlugin:
    """Propose one ordinary follow-up task after a method task completes."""

    plugin_id = "continuation.publish.v1"

    def propose(self, request: PluginRequest) -> PluginProposal:
        if request.source is None:
            raise ValueError("continuation publication requires a source Contract")
        method = str(method_payload(request.source).get("method", "method"))
        continuation = continuation_contract(
            request.parent,
            request.source,
            method=method,
            budget=max(1, request.allowance),
        )
        return PluginProposal(effects=(contract_declaration_effect(continuation),))
