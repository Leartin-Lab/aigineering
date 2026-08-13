"""Offline proof of the complete LLM -> tool -> audit AI4S example."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path

from click.testing import CliRunner
import pytest

from aigineering.agent.llm import LLMWorker
from aigineering.agent.tool_registry_loader import (
    load_tool_registry,
    provider_tool_definitions,
)
from aigineering.cli.main import cli
from aigineering.cli.run import _PoolHost, _claim_for_pool_host
from aigineering.agent.tool_worker import ToolWorker
from aigineering.local_identity import ensure_local_worker_host
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.cli.task_state import project_task_status

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "examples/literature-evidence/assets/openalex-response.json"
DESCRIPTION = ROOT / "examples/ai4s/task-description.txt"
TOOLS = ROOT / "examples/ai4s/tools.py"
AUDIT = ROOT / "examples/ai4s/audit.py"


def _audit_module():
    spec = importlib.util.spec_from_file_location("ai4s_audit", AUDIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registry_loader_exposes_specs_without_handlers():
    registry = load_tool_registry(f"{TOOLS}:build_registry")
    definitions = provider_tool_definitions(registry)

    assert [item["function"]["name"] for item in definitions] == ["openalex_search"]
    assert "handler" not in json.dumps(definitions)


@pytest.mark.parametrize(
    "reference",
    ("missing-separator", "examples.ai4s.tools:missing", "json:loads"),
)
def test_registry_loader_fails_closed(reference):
    with pytest.raises(ValueError):
        load_tool_registry(reference)


def test_report_verifier_rejects_fabricated_citation():
    verify_report = _audit_module().verify_report
    manifests = [{"records": [{"id": "https://openalex.org/W1", "title": "Evidence"}]}]
    valid = json.dumps(
        {
            "answer": "Bounded answer.",
            "citations": ["https://openalex.org/W1"],
            "limitations": ["Metadata only."],
        }
    )
    assert verify_report(valid, manifests)["citation_count"] == 1

    forged = valid.replace("W1", "W999")
    with pytest.raises(ValueError, match="absent from retrieved evidence"):
        verify_report(forged, manifests)


def test_tool_execution_lane_does_not_claim_generic_task():
    runner = CliRunner()
    with runner.isolated_filesystem():
        assert runner.invoke(cli, ["domain", "init"]).exit_code == 0
        created = runner.invoke(
            cli,
            ["task", "create", "--name", "generic", "--output", "result", "--json"],
        )
        assert created.exit_code == 0, created.output
        store = SQLiteStore(".aig/store.db")
        registry = load_tool_registry(f"{TOOLS}:build_registry")
        host = ensure_local_worker_host(store, ToolWorker(registry))

        assert (
            _claim_for_pool_host(store, _PoolHost(host, "tool-execution"), None) is None
        )
        assert not store.scan_runtime_records(record_type="claim.granted")
        store.close()


def test_offline_llm_tool_report_requires_distinct_audit(monkeypatch):
    attest_report = _audit_module().attest_report
    responses = iter(
        (
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "openalex_search",
                                        "arguments": json.dumps(
                                            {
                                                "query": "scientific RAG",
                                                "max_records": 2,
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
            {
                "choices": [
                    {
                        "message": {
                            "content": "/exec "
                            + json.dumps(
                                {
                                    "outputs": {
                                        "literature_report": json.dumps(
                                            {
                                                "answer": (
                                                    "The retrieved metadata shows "
                                                    "scientific RAG work."
                                                ),
                                                "citations": [
                                                    "https://openalex.org/W0000000001"
                                                ],
                                                "limitations": [
                                                    "Metadata alone does not validate "
                                                    "scientific claims."
                                                ],
                                            },
                                            sort_keys=True,
                                        )
                                    }
                                },
                                sort_keys=True,
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 140, "completion_tokens": 50},
            },
        )
    )

    def fake_call(self, url, headers, payload):
        del self, url, headers, payload
        return next(responses)

    monkeypatch.setattr(LLMWorker, "_call", fake_call)
    runner = CliRunner()
    with runner.isolated_filesystem():
        assert runner.invoke(cli, ["domain", "init"]).exit_code == 0
        policy = json.dumps(
            {
                "mode": "independent",
                "policy_version": "ai4s-literature-v1",
                "required_attestations": 1,
                "verifier_capabilities": ["verify.literature"],
            },
            separators=(",", ":"),
        )
        created = runner.invoke(
            cli,
            [
                "task",
                "create",
                "--name",
                "ai4s_literature_report",
                "--description-file",
                str(DESCRIPTION),
                "--output",
                "literature_report",
                "--budget",
                "4",
                "--tool",
                "openalex_search",
                "--acceptance-policy",
                policy,
                "--json",
            ],
        )
        assert created.exit_code == 0, created.output
        task_id = json.loads(created.output)["contract_id"]
        run = runner.invoke(
            cli,
            [
                "run",
                "--task",
                task_id,
                "--worker",
                "llm",
                "--model",
                "fixture-model",
                "--tool-registry",
                f"{TOOLS}:build_registry",
                "--wait-timeout",
                "5",
                "--interval",
                "0.05",
                "--json",
            ],
            env={
                "AIGINEERING_API_KEY": "fixture-key",
                "AIGINEERING_AI4S_OPENALEX_FIXTURE": str(FIXTURE),
                "AIGINEERING_AI4S_RETRIEVED_AT": "2026-08-13T00:00:00+00:00",
            },
        )
        assert run.exit_code == 1, f"{run.output}\n{run.exception!r}"
        assert run.output, repr(run.exception)
        before = json.loads(run.output)
        assert before["outputs"]["literature_report"]
        assert before["outputs_satisfied"] is False

        evidence = attest_report(".aig/store.db", task_id)
        assert evidence["accepted"] is True
        assert evidence["citation_count"] == 1
        assert evidence["observation_asset_ids"]

        reopened = SQLiteStore(".aig/store.db")
        task = reopened.get_contract(task_id)
        assert task is not None
        after = project_task_status(task, reopened)
        assert after["status"] == "completed"
        assert after["outputs_satisfied"] is True
        assert reopened.scan_runtime_records(record_type="asset.attested")
        reopened.close()
