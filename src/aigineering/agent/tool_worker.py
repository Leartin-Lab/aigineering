"""ToolWorker — boundary wrapper that exposes tool execution through the Worker protocol (v0.5.0).

Wraps ToolRegistry as a proper Worker protocol implementation. Tool execution
follows the same candidate → projection → authority → trace boundary as LLM
and mock workers.  Produces ``_tool_obs_*`` observation assets (observations,
not outputs — see ADR-006).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aigineering.agent.tool_executor import ToolExecutor
from aigineering.core.capability_descriptors import verify_descriptor
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.immutability import deep_thaw
from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.types import Candidate

if TYPE_CHECKING:
    from aigineering.core.tools import ToolRegistry
    from aigineering.protocol.types import Asset, Contract


class ToolWorker:
    """Worker that executes tool calls through ToolRegistry.

    ToolWorker is a boundary wrapper: it presents tool execution as a
    standard Worker protocol implementation.  The low-level ToolExecutor
    remains an internal adapter.  ToolWorker never commits directly —
    it returns a Candidate that must pass through projection.
    """

    worker_id: str

    def __init__(
        self,
        registry: ToolRegistry,
        worker_id: str = "tool_worker",
        *,
        pools: tuple[str, ...] = (),
        capacity: int = 1,
        profile_id: str = "tool-worker-v1",
        routing_capabilities: tuple[str, ...] = (),
        registration_version: str = "1",
    ) -> None:
        self._registry = registry
        self._executor = ToolExecutor(registry)
        self.worker_id = worker_id
        self._pools = tuple(pools)
        self._capacity = capacity
        self._profile_id = profile_id
        self._routing_capabilities = tuple(routing_capabilities)
        self._registration_version = registration_version

    def registration(self) -> WorkerRegistration:
        """Route this Worker only to ordinary tool-execution Contracts."""
        capabilities = (
            "tool-execution",
            *(f"tool:{spec.name}" for spec in self._registry.list_specs()),
            *self._routing_capabilities,
        )
        return WorkerRegistration(
            self.worker_id,
            capabilities=capabilities,
            pools=self._pools,
            profile_id=self._profile_id,
            capacity=self._capacity,
            version=self._registration_version,
        )

    def invoke(
        self,
        contract: Contract,
        disclosed_assets: list[Asset],
    ) -> Candidate:
        """Execute a tool request from *contract* and return a Candidate.

        Parses the tool name and arguments from the contract's method
        payload, executes through ToolRegistry, and returns a Candidate
        whose raw_output is a JSON observation object with fields
        ``ok``, ``tool``, ``result``, and ``error``.
        """
        payload = method_payload(contract)
        tool_payload = (
            payload.get("payload", {})
            if isinstance(payload.get("payload"), dict)
            else {}
        )
        tool_name = tool_payload.get("name", "")
        args = tool_payload.get("args", {})

        if len(contract.outputs) != 1:
            return self._executor.error_candidate(
                str(tool_name),
                contract.id,
                "tool method contract must declare exactly one observation output",
                error_type="ToolContractError",
                worker_id=self.worker_id,
            )
        if not isinstance(tool_name, str) or not tool_name:
            return self._executor.error_candidate(
                str(tool_name),
                contract.id,
                "tool action missing string payload.name",
                error_type="ToolActionError",
                worker_id=self.worker_id,
            )

        descriptor_name = f"_tool_capability_{tool_name}"
        descriptor = next(
            (asset for asset in disclosed_assets if asset.name == descriptor_name),
            None,
        )
        if tool_name not in contract.tool_scope:
            return _wrap_execution_candidate(
                self.worker_id,
                contract,
                self._executor.error_candidate(
                    tool_name,
                    contract.id,
                    f"tool '{tool_name}' is not in contract.tool_scope",
                    error_type="ToolScopeError",
                    worker_id=self.worker_id,
                ),
            )
        elif descriptor is None or not verify_descriptor(descriptor, kind="tool"):
            return _wrap_execution_candidate(
                self.worker_id,
                contract,
                self._executor.error_candidate(
                    tool_name,
                    contract.id,
                    f"tool descriptor '{descriptor_name}' is missing or invalid",
                    error_type="ToolCapabilityError",
                    worker_id=self.worker_id,
                ),
            )
        elif not _descriptor_matches_spec(
            descriptor.content, tool_name, self._registry.get_spec(tool_name)
        ):
            return _wrap_execution_candidate(
                self.worker_id,
                contract,
                self._executor.error_candidate(
                    tool_name,
                    contract.id,
                    f"tool descriptor '{descriptor_name}' does not match the registered tool contract",
                    error_type="ToolCapabilityDriftError",
                    worker_id=self.worker_id,
                ),
            )
        else:
            executed = self._executor.invoke(
                tool_name,
                args if isinstance(args, dict) else {},
                contract.id,
            )
            return _wrap_execution_candidate(self.worker_id, contract, executed)


def _descriptor_matches_spec(content: str, tool_name: str, spec: object) -> bool:
    """Ensure the signed capability binds the exact executable tool contract."""
    if spec is None:
        return False
    try:
        descriptor = json.loads(content)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(descriptor, dict)
        and descriptor.get("kind") == "tool"
        and descriptor.get("name") == tool_name
        and descriptor.get("version", "0.1.0") == spec.version
        and _schemas_equal(
            descriptor.get("input_schema", {}), deep_thaw(spec.input_schema)
        )
        and _schemas_equal(
            descriptor.get("output_schema", {}), deep_thaw(spec.output_schema)
        )
        and descriptor.get("max_output_bytes", 1_048_576) == spec.max_output_bytes
    )


def _schemas_equal(descriptor_schema: object, registered_schema: object) -> bool:
    if descriptor_schema == registered_schema:
        return True
    # Older descriptors commonly declared the tool payload as an object while
    # ToolSpec's historical default was the unconstrained schema {}.
    return registered_schema == {} and descriptor_schema == {"type": "object"}


def _observation_candidate(
    worker_id: str, contract: Contract, observation: str
) -> Candidate:
    outputs = {contract.outputs[0]: observation}
    return Candidate(
        worker_id=worker_id,
        raw_output=json.dumps(
            {"type": "exec", "outputs": outputs},
            sort_keys=True,
            ensure_ascii=False,
        ),
        parsed_action={"type": "exec", "outputs": outputs},
    )


def _wrap_execution_candidate(
    worker_id: str, contract: Contract, executed: Candidate
) -> Candidate:
    wrapped = _observation_candidate(worker_id, contract, executed.raw_output)
    return Candidate(
        worker_id=wrapped.worker_id,
        raw_output=wrapped.raw_output,
        parsed_action=wrapped.parsed_action,
        metadata=executed.metadata,
    )
