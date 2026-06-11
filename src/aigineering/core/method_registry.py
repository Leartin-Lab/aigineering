"""Method handler protocol and registry for v0.3.3+ dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aigineering.core.engine import Engine
    from aigineering.protocol.types import Asset, Candidate, Contract


class MethodHandler(Protocol):
    """A handler for a specific method action type (plan, tool, replan, etc)."""

    def can_handle(self, action_type: str) -> bool:
        """Return True if this handler can process the given action type."""
        ...

    def handle_method(
        self,
        engine: Engine,
        contract: Contract,
        action_type: str,
        candidate: Candidate,
    ) -> bool:
        """Handle a method action.

        Return True if the method was handled (and parent should be suspended).
        Return False to let the engine use default scheduling behavior.
        """
        ...

    def handle_completion(
        self,
        engine: Engine,
        contract: Contract,
        method_assets: list[Asset],
    ) -> bool:
        """Handle method contract completion. Return True if expansion was performed.

        Called by :meth:`Engine._resume_parent_from_method` when a system
        method contract completes.  The handler may expand plan results,
        process tool observations, etc.
        """
        ...


class MethodRegistry:
    """Registry for method action handlers.

    Maps action type strings (``"plan"``, ``"tool"``, ``"replan"``) to
    :class:`MethodHandler` instances.  Handlers registered for a given type
    take priority over the built-in inline dispatch.  When a handler's
    :meth:`MethodHandler.can_handle` returns ``True`` the engine calls
    :meth:`MethodHandler.handle_method` and skips the default scheduling.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, MethodHandler] = {}

    def register(self, action_type: str, handler: MethodHandler) -> None:
        """Register *handler* for *action_type*.

        Overwrites any previously-registered handler for the same type.
        """
        self._handlers[action_type] = handler

    def deregister(self, action_type: str) -> None:
        """Remove the handler for *action_type*, if one is registered."""
        self._handlers.pop(action_type, None)

    def get(self, action_type: str) -> MethodHandler | None:
        """Return the handler registered for *action_type*, or ``None``."""
        return self._handlers.get(action_type)

    def list_types(self) -> list[str]:
        """Return the sorted list of registered action types."""
        return sorted(self._handlers.keys())
