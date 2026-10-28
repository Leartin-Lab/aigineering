"""Local factory adapters that preserve the ordinary LLMWorker path."""

from __future__ import annotations

from aigineering.agent.llm import LLMWorker

from .runtime_meter import _complete_role, create_transport, factory_construction


def _make(role: str, worker_id: str) -> LLMWorker:
    with factory_construction():
        return _make_locked(role, worker_id)


def _make_locked(role: str, worker_id: str) -> LLMWorker:
    session, api_key, transport = create_transport(role, worker_id)
    config = session.config
    researcher = role == "researcher"
    try:
        worker = LLMWorker(
            model=config.model,
            api_key=api_key,
            base_url=config.base_url,
            worker_id=worker_id,
            transport=transport,
            timeout=config.request_timeout_seconds,
            max_retries=0,
            capabilities=frozenset({"tool_calling"}) if researcher else frozenset(),
            tool_definitions=session.tool_definitions if researcher else None,
            routing_capabilities=frozenset(
                {"calibration.research" if researcher else "calibration.plan"}
            ),
            worker_pools=frozenset({"research" if researcher else "planning"}),
            profile_id="openai-compatible-v1",
            capacity=1,
            registration_version="1",
            max_output_tokens=config.max_output_tokens,
            thinking_mode="disabled",
            response_format="" if researcher else "json_object",
        )
    except Exception:
        _complete_role(session, role, succeeded=False)
        raise
    _complete_role(session, role, succeeded=True)
    return worker


def create_metered_planner(*, worker_id: str) -> LLMWorker:
    return _make("planner", worker_id)


def create_metered_researcher(*, worker_id: str) -> LLMWorker:
    return _make("researcher", worker_id)
