from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from click.testing import CliRunner
import pytest

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Asset
from examples.thermal_calibration import runtime_meter

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "thermal_calibration"


def _require_g0c_test_redis(monkeypatch):
    allocated = os.environ.get("AIG_REDIS_TEST_URL")
    selected = os.environ.get("AIGINEERING_G0C_TEST_REDIS_URL")
    redis_url = selected if selected is not None else allocated
    if redis_url is None:
        pytest.skip("metered CLI test requires an explicitly allocated test Redis URL")
    try:
        parsed = urlsplit(redis_url)
        host = parsed.hostname
        port = parsed.port
        database = parsed.path.removeprefix("/")
        if (
            parsed.scheme not in {"redis", "rediss"}
            or not host
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not database.isascii()
            or not database.isdecimal()
        ):
            raise ValueError("invalid test Redis URL")
        display_host = f"[{host}]" if ":" in host else host
        canonical = f"{parsed.scheme}://{display_host}:{port}/{int(database)}"
        if redis_url != canonical or (
            allocated is not None and selected is not None and allocated != selected
        ):
            raise ValueError("test Redis allocation mismatch")
    except ValueError:
        pytest.fail(
            "test Redis URL must be canonical and match its explicit allocation"
        )
    monkeypatch.setenv("AIGINEERING_REDIS_URL", redis_url)


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://user@127.0.0.1:32770/7",
        "redis://127.0.0.1:32770/7?db=0",
        "redis://127.0.0.1:32770/7#fragment",
        "redis://127.0.0.1:32770/07",
        "redis://127.0.0.1:32770/7/",
    ],
)
def test_cli_redis_guard_rejects_noncanonical_addresses_before_client(
    monkeypatch, redis_url
):
    monkeypatch.setenv("AIGINEERING_G0C_TEST_REDIS_URL", redis_url)
    monkeypatch.setenv("AIG_REDIS_TEST_URL", redis_url)
    monkeypatch.delenv("AIGINEERING_REDIS_URL", raising=False)
    with pytest.raises(pytest.fail.Exception):
        _require_g0c_test_redis(monkeypatch)
    assert "AIGINEERING_REDIS_URL" not in os.environ


@pytest.mark.parametrize(
    "redis_url", ["redis://127.0.0.1:32771/7", "redis://127.0.0.1:32770/8"]
)
def test_cli_redis_guard_accepts_explicit_allocated_resources(monkeypatch, redis_url):
    monkeypatch.setenv("AIG_REDIS_TEST_URL", redis_url)
    monkeypatch.setenv("AIGINEERING_G0C_TEST_REDIS_URL", redis_url)
    monkeypatch.delenv("AIGINEERING_REDIS_URL", raising=False)
    _require_g0c_test_redis(monkeypatch)
    assert os.environ["AIGINEERING_REDIS_URL"] == redis_url


def test_cli_redis_guard_uses_standard_test_allocation_not_application_default(
    monkeypatch,
):
    monkeypatch.delenv("AIGINEERING_G0C_TEST_REDIS_URL", raising=False)
    monkeypatch.setenv("AIG_REDIS_TEST_URL", "redis://127.0.0.1:32771/15")
    monkeypatch.setenv("AIGINEERING_REDIS_URL", "redis://127.0.0.1:6379/0")
    _require_g0c_test_redis(monkeypatch)
    assert os.environ["AIGINEERING_REDIS_URL"] == "redis://127.0.0.1:32771/15"


@pytest.mark.parametrize("selected", ["redis://127.0.0.1:32770/7", ""])
def test_cli_redis_guard_refuses_conflicting_explicit_allocations(
    monkeypatch, selected
):
    monkeypatch.setenv("AIGINEERING_G0C_TEST_REDIS_URL", selected)
    monkeypatch.setenv("AIG_REDIS_TEST_URL", "redis://127.0.0.1:32771/15")
    monkeypatch.delenv("AIGINEERING_REDIS_URL", raising=False)
    with pytest.raises(pytest.fail.Exception):
        _require_g0c_test_redis(monkeypatch)
    assert "AIGINEERING_REDIS_URL" not in os.environ


def test_cli_redis_guard_requires_test_resource_instead_of_application_default(
    monkeypatch,
):
    monkeypatch.delenv("AIGINEERING_G0C_TEST_REDIS_URL", raising=False)
    monkeypatch.delenv("AIG_REDIS_TEST_URL", raising=False)
    monkeypatch.setenv("AIGINEERING_REDIS_URL", "redis://127.0.0.1:6379/0")
    with pytest.raises(pytest.skip.Exception):
        _require_g0c_test_redis(monkeypatch)
    assert os.environ["AIGINEERING_REDIS_URL"] == "redis://127.0.0.1:6379/0"


def test_source_checkout_pythonpath_prepares_example_registry_offline(
    tmp_path, monkeypatch
):
    probe = tmp_path / "prepare_source_profile.py"
    probe.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import socket",
                "import sys",
                f"root = Path({str(ROOT)!r})",
                "def deny_network(*args, **kwargs):",
                "    raise AssertionError('offline preparation attempted network I/O')",
                "socket.socket.connect = deny_network",
                "socket.create_connection = deny_network",
                "import aigineering.cli.main as cli_main",
                "assert Path(cli_main.__file__).is_relative_to(root / 'src')",
                "assert root in {Path(item).resolve() for item in sys.path if item}",
                "from aigineering.run_config import prepare_configured_run",
                "prepared = prepare_configured_run(root / 'examples/thermal_calibration/run-g0c.toml')",
                "assert prepared is not None",
                "print('source profile prepared with example registry')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    child_env = os.environ.copy()
    for key in tuple(child_env):
        if any(
            token in key.upper() for token in ("API_KEY", "SECRET", "TOKEN", "REDIS")
        ):
            child_env.pop(key)
    child_env["AIGINEERING_API_KEY"] = "dummy-g0c-prepare-key"
    child_env["AIGINEERING_G0C_MAX_ATTEMPTS"] = "8"
    child_env["AIGINEERING_G0C_OUTPUT_RESERVATION_CEILING"] = "65536"
    child_env["AIGINEERING_G0C_RUN_DEADLINE_SECONDS"] = "900"
    child_env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    prepared = subprocess.run(
        [sys.executable, str(probe)],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    assert "source profile prepared with example registry" in prepared.stdout


def _prompt_assets(prompt: str) -> list[Asset]:
    marker = "Disclosed assets:\n"
    if marker not in prompt:
        return []
    section = prompt.split(marker, 1)[1]
    matches = list(re.finditer(r"^- ([^\[\n]+) \[([^\]\n]+)\]: ", section, re.M))
    assets = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        content_start = match.end()
        content = section[content_start:end].rstrip("\n")
        assets.append(
            Asset(id=match.group(2), name=match.group(1).strip(), content=content)
        )
    return assets


def _json_action(action_type: str, payload: dict) -> str:
    return json.dumps({"type": action_type, "payload": payload}, separators=(",", ":"))


def _exec_action(outputs: dict[str, str]) -> str:
    return json.dumps(
        {"type": "exec", "outputs": outputs}, ensure_ascii=False, separators=(",", ":")
    )


def _provider_reply(payload: dict) -> dict:
    prompt = payload["messages"][1]["content"]
    output = re.search(r"^Declared outputs: (.+)$", prompt, re.M)
    output_name = output.group(1).split(", ")[0]

    if output_name.startswith("_fail_result_"):
        action = _exec_action(
            {
                output_name: json.dumps(
                    {
                        "method": "fail",
                        "reason": "the provider attempt failed and needs exact recovery evidence",
                    },
                    sort_keys=True,
                )
            }
        )
        return {
            "choices": [{"message": {"content": action}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

    if re.search(
        r"^Contract name: synthetic_calibration_drift_review(?:\.recover)*$",
        prompt,
        re.M,
    ):
        content = _json_action(
            "plan",
            {"reason": "derive the report using both scoped tools, then verify it"},
        )
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

    if "Planning draft protocol" in prompt:
        draft = {
            "goals": [
                "compute per-sample calibration error and drift",
                "independently verify the report and source evidence",
            ],
            "evidence_needs": [
                "the exact case and standard",
                "both complete tool observations",
            ],
            "uncertainties": [
                "all measurements are synthetic and do not establish real-world calibration"
            ],
            "proposed_steps": [
                "normalize source values",
                "analyze the normalized source",
                "independently evaluate and attest the report",
            ],
        }
        return {
            "choices": [
                {
                    "message": {
                        "content": _exec_action(
                            {output_name: json.dumps(draft, sort_keys=True)}
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

    if "Planning dependency protocol" in prompt:
        dependencies = {
            "producers": ["calibration research", "independent verifier"],
            "consumers": [
                "research consumes both source assets",
                "verifier consumes sources, report, and evidence",
            ],
            "missing_inputs": [],
            "cycles": [],
            "parallel_groups": [],
            "capability_needs": ["calibration.research", "calibration.verify"],
            "authority_risks": [],
            "allowance_needs": ["research requires two tool calls and continuations"],
        }
        return {
            "choices": [
                {
                    "message": {
                        "content": _exec_action(
                            {output_name: json.dumps(dependencies, sort_keys=True)}
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

    if (
        "Planner result protocol" in prompt
        or "temporary output named planning_blueprint" in prompt
    ):
        parent = re.search(r"^Parent Contract ID: (.+)$", prompt, re.M).group(1)
        assets = {asset.name: asset for asset in _prompt_assets(prompt)}
        blueprint = {
            "contracts": [
                {
                    "name": "calibration_research",
                    "description": "Use the exact calibration inputs and both committed tool observations to produce the report and evidence.",
                    "inputs": ["calibration_case", "calibration_standard"],
                    "outputs": ["calibration_report", "calibration_evidence"],
                    "activation": "calibration_case AND calibration_standard",
                    "budget": 16,
                    "tool_scope": ["normalize_calibration", "analyze_calibration"],
                    "labels": ["_skill_content_thermal_calibration"],
                    "capability_needs": ["calibration.research"],
                    "pool_needs": ["research"],
                    "delegation_capabilities": [],
                    "delegation_pools": [],
                },
                {
                    "name": "calibration_verifier",
                    "description": json.dumps(
                        {
                            "schema": "thermal-calibration-verifier-target-v1",
                            "root_contract_id": parent,
                            "calibration_case_asset_id": assets["calibration_case"].id,
                            "calibration_standard_asset_id": assets[
                                "calibration_standard"
                            ].id,
                            "policy_version": "synthetic-temperature-calibration-v1",
                        },
                        sort_keys=True,
                    ),
                    "inputs": [
                        "calibration_case",
                        "calibration_standard",
                        "calibration_report",
                        "calibration_evidence",
                    ],
                    "outputs": ["calibration_receipt"],
                    "activation": "calibration_case AND calibration_standard AND calibration_report AND calibration_evidence",
                    "budget": 1,
                    "tool_scope": [],
                    "labels": ["_skill_content_thermal_calibration"],
                    "capability_needs": ["calibration.verify"],
                    "pool_needs": ["verification"],
                    "delegation_capabilities": [],
                    "delegation_pools": [],
                },
            ]
        }
        return {
            "choices": [
                {
                    "message": {
                        "content": _exec_action(
                            {
                                "planning_blueprint": json.dumps(
                                    blueprint, sort_keys=True
                                )
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

    if "Contract name: calibration_research" in prompt:
        assets = _prompt_assets(prompt)
        successful = {
            observation["tool"]: (asset, observation)
            for asset, observation in __import__(
                "tests.fixtures.thermal_calibration_workers",
                fromlist=["_observation_assets"],
            )._observation_assets(assets)
            if observation.get("ok") is True
        }
        if "normalize_calibration" not in successful:
            case = json.loads(
                next(
                    asset.content
                    for asset in assets
                    if asset.name == "calibration_case"
                )
            )
            standard = json.loads(
                next(
                    asset.content
                    for asset in assets
                    if asset.name == "calibration_standard"
                )
            )
            calls = [
                {
                    "id": "fake-normalize",
                    "type": "function",
                    "function": {
                        "name": "normalize_calibration",
                        "arguments": json.dumps({"case": case, "standard": standard}),
                    },
                }
            ]
            return {
                "choices": [{"message": {"tool_calls": calls}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
        if "analyze_calibration" not in successful:
            normalized = json.loads(successful["normalize_calibration"][1]["result"])
            calls = [
                {
                    "id": "fake-analyze",
                    "type": "function",
                    "function": {
                        "name": "analyze_calibration",
                        "arguments": json.dumps({"normalized": normalized}),
                    },
                }
            ]
            return {
                "choices": [{"message": {"tool_calls": calls}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
        report = successful["analyze_calibration"][1]["result"]
        by_tool = {
            name: successful[name][0]
            for name in ("normalize_calibration", "analyze_calibration")
        }
        observations = {name: successful[name][1] for name in by_tool}
        case_asset = next(asset for asset in assets if asset.name == "calibration_case")
        standard_asset = next(
            asset for asset in assets if asset.name == "calibration_standard"
        )
        evidence = {
            "schema": "thermal-calibration-evidence-v1",
            "calibration_case_asset_id": case_asset.id,
            "calibration_standard_asset_id": standard_asset.id,
            "observation_asset_ids": [
                by_tool[name].id
                for name in ("normalize_calibration", "analyze_calibration")
            ],
            "observations": [
                {"asset_id": by_tool[name].id, "observation": observations[name]}
                for name in ("normalize_calibration", "analyze_calibration")
            ],
        }
        content = "/exec " + json.dumps(
            {
                "outputs": {
                    "calibration_report": report,
                    "calibration_evidence": json.dumps(evidence, sort_keys=True),
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

    diagnostic = " | ".join(
        line
        for line in prompt.splitlines()
        if line.startswith(
            (
                "Contract name:",
                "Declared outputs:",
                "Allowed tools:",
                "Planner result protocol",
            )
        )
    )
    raise AssertionError("unexpected fake provider prompt category: " + diagnostic)


def test_ordinary_cli_runtime_metered_profile_completes_and_is_independently_accepted(
    tmp_path, monkeypatch
):
    _require_g0c_test_redis(monkeypatch)
    monkeypatch.setenv("AIGINEERING_API_KEY", "dummy-g0c-cli-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AIGINEERING_G0C_MAX_ATTEMPTS", "8")
    monkeypatch.setenv("AIGINEERING_G0C_OUTPUT_RESERVATION_CEILING", "65536")
    monkeypatch.setenv("AIGINEERING_G0C_RUN_DEADLINE_SECONDS", "180")
    runtime_meter.reset_process_session_for_tests()
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.syspath_prepend(str(ROOT / "tests"))
    monkeypatch.syspath_prepend(str(ROOT / "tests" / "fixtures"))
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            item for item in (str(ROOT), str(ROOT / "tests"), existing) if item
        ),
    )
    provider_payloads = []

    def fake_provider_transport(**kwargs):
        body = json.loads(kwargs["body"])
        provider_payloads.append(body)
        return runtime_meter.TransportReply(
            200, json.dumps(_provider_reply(body)).encode()
        )

    monkeypatch.setattr(
        runtime_meter, "_stdlib_https_transport", fake_provider_transport
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    preset = tmp_path / "preset"
    shutil.copytree(EXAMPLE, preset)
    config = preset / "run-g0c.toml"
    result = CliRunner().invoke(
        cli,
        [
            "-C",
            str(workspace),
            "run",
            "--config",
            str(config),
            "--wait-timeout",
            "60",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["completed"] is True
    assert 1 <= len(provider_payloads) <= 8
    assert all(item["max_tokens"] == 8192 for item in provider_payloads)
    planner_payloads = [item for item in provider_payloads if "response_format" in item]
    researcher_payloads = [
        item for item in provider_payloads if "response_format" not in item
    ]
    assert planner_payloads
    assert researcher_payloads
    assert all(item["thinking"] == {"type": "disabled"} for item in provider_payloads)
    assert all(
        item.get("response_format") == {"type": "json_object"}
        for item in planner_payloads
    )
    tool_payloads = [item for item in researcher_payloads if "tools" in item]
    assert tool_payloads
    tool_names = {
        tool["function"]["name"] for item in tool_payloads for tool in item["tools"]
    }
    assert tool_names == {"normalize_calibration", "analyze_calibration"}
    store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        report = next(asset for asset in store.get_assets_by_name("calibration_report"))
        evidence = list(store.get_assets_by_name("calibration_evidence"))
        assert len(evidence) == 1
        evidence_value = json.loads(evidence[0].content)
        assert set(evidence_value) == {
            "schema",
            "calibration_case_asset_id",
            "calibration_standard_asset_id",
            "observation_asset_ids",
            "observations",
        }
        assert evidence_value["schema"] == "thermal-calibration-evidence-v1"
        assert len(evidence_value["observation_asset_ids"]) == 2
        assert len(set(evidence_value["observation_asset_ids"])) == 2
        assert len(evidence_value["observations"]) == 2
        assert all(
            set(item) == {"asset_id", "observation"}
            and isinstance(item["observation"], dict)
            for item in evidence_value["observations"]
        )
        case = store.get_assets_by_name("calibration_case")
        standard = store.get_assets_by_name("calibration_standard")
        assert len(case) == len(standard) == 1
        assert evidence_value["calibration_case_asset_id"] == case[0].id
        assert evidence_value["calibration_standard_asset_id"] == standard[0].id
        assert [item["asset_id"] for item in evidence_value["observations"]] == (
            evidence_value["observation_asset_ids"]
        )
        root_id = payload["contract_id"]
        attested = [
            record
            for _, record in store.scan_runtime_records(record_type="asset.attested")
            if record.payload.get("contract_id") == root_id
            and record.payload.get("output_name") == "calibration_report"
        ]
        qualified = [
            record
            for _, record in store.scan_runtime_records(record_type="output.qualified")
            if record.payload.get("contract_id") == root_id
            and record.payload.get("output_name") == "calibration_report"
        ]
        assert len(attested) == len(qualified) == 1
        assert attested[0].payload["asset_id"] == report.id
        assert attested[0].payload["verdict"] == "accepted"
        assert attested[0].payload["evidence_asset_ids"] == tuple(
            sorted((case[0].id, standard[0].id))
        )
        logs = list((workspace / ".aig" / "worker-logs").glob("*.jsonl"))
        event_text = "\n".join(path.read_text(encoding="utf-8") for path in logs)
        assert "provider.permit_admitted" in event_text
        assert "provider.transport_entered" in event_text
        assert "provider.attempt_finalized" in event_text
        assert "dummy-g0c-cli-key" not in event_text
        assert "dummy-g0c-cli-key" not in result.output
    finally:
        store.close()
    runtime_meter.reset_process_session_for_tests()


def test_ordinary_cli_provider_http_failure_records_attempt_without_unfunded_recovery(
    tmp_path, monkeypatch
):
    _require_g0c_test_redis(monkeypatch)
    monkeypatch.setenv("AIGINEERING_API_KEY", "dummy-g0c-cli-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AIGINEERING_G0C_MAX_ATTEMPTS", "16")
    monkeypatch.setenv("AIGINEERING_G0C_OUTPUT_RESERVATION_CEILING", "131072")
    monkeypatch.setenv("AIGINEERING_G0C_RUN_DEADLINE_SECONDS", "900")
    runtime_meter.reset_process_session_for_tests()
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.syspath_prepend(str(ROOT / "tests"))
    monkeypatch.syspath_prepend(str(ROOT / "tests" / "fixtures"))
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            item for item in (str(ROOT), str(ROOT / "tests"), existing) if item
        ),
    )
    calls = 0

    def fail_first_provider_call(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return runtime_meter.TransportReply(
                503, b'{"error":"dummy failure","usage":{"prompt_tokens":3}}'
            )
        return runtime_meter.TransportReply(
            200, json.dumps(_provider_reply(json.loads(kwargs["body"]))).encode()
        )

    monkeypatch.setattr(
        runtime_meter, "_stdlib_https_transport", fail_first_provider_call
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    preset = tmp_path / "preset"
    shutil.copytree(EXAMPLE, preset)
    config = preset / "run-g0c.toml"
    result = CliRunner().invoke(
        cli,
        [
            "-C",
            str(workspace),
            "run",
            "--config",
            str(config),
            "--wait-timeout",
            "15",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    root_id = payload["contract_id"]
    store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        contracts = {contract.id: contract for contract in store.get_all_contracts()}
        root_failures = [
            record
            for _, record in store.scan_runtime_records(
                record_type="worker.invocation_failed"
            )
            if record.payload.get("contract_id") == root_id
        ]
        assert len(root_failures) == 1
        assert root_failures[0].payload["status_code"] == 503
        assert not store.scan_runtime_records(
            record_type="worker_failure.recovery_scheduled"
        )
        assert not any(contract.parent_id == root_id for contract in contracts.values())
        assert not store.scan_runtime_records(record_type="lifecycle.terminal")
        extinguished = [
            record
            for _, record in store.scan_runtime_records(
                record_type="allowance.extinguished"
            )
            if record.payload.get("contract_id") == root_id
        ]
        assert len(extinguished) == 1
        root = contracts[root_id]
        assert extinguished[0].payload["amount"] == root.budget
        assert calls == 1
        logs = list((workspace / ".aig" / "worker-logs").glob("*.jsonl"))
        event_text = "\n".join(path.read_text(encoding="utf-8") for path in logs)
        root_final_events = []
        for path in logs:
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            if rows and rows[0].get("contract_id") == root_id:
                root_final_events.extend(
                    row["fields"]
                    for row in rows
                    if row.get("event") == "provider.attempt_finalized"
                )
        final_events = [
            json.loads(line)["fields"]
            for path in logs
            for line in path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "provider.attempt_finalized"
        ]
        assert any(
            row["outcome"] == "failed"
            and row["status_code"] == 503
            and row["prompt_tokens"] == 3
            and row["prompt_status"] == "reported"
            for row in final_events
        )
        initial_epochs = {row["meter_epoch_id"] for row in root_final_events}
        assert len(initial_epochs) == 1
        assert "provider.response_observed" in event_text
        assert "provider.attempt_finalized" in event_text
        assert "dummy-g0c-cli-key" not in event_text
        assert "dummy failure" not in event_text
        assert "dummy-g0c-cli-key" not in result.output
    finally:
        store.close()
    runtime_meter.reset_process_session_for_tests()


@pytest.mark.parametrize(
    ("case", "attempts", "ceiling", "deadline"),
    [
        ("zero-attempts", "0", "131072", "900"),
        ("zero-ceiling", "16", "0", "900"),
        ("expired-deadline", "16", "131072", "0.001"),
    ],
)
def test_ordinary_cli_meter_limits_fail_without_transport_or_unfunded_recovery(
    tmp_path, monkeypatch, case, attempts, ceiling, deadline
):
    _require_g0c_test_redis(monkeypatch)
    monkeypatch.setenv("AIGINEERING_API_KEY", "dummy-g0c-cli-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AIGINEERING_G0C_MAX_ATTEMPTS", attempts)
    monkeypatch.setenv("AIGINEERING_G0C_OUTPUT_RESERVATION_CEILING", ceiling)
    monkeypatch.setenv("AIGINEERING_G0C_RUN_DEADLINE_SECONDS", deadline)
    runtime_meter.reset_process_session_for_tests()
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.syspath_prepend(str(ROOT / "tests"))
    monkeypatch.syspath_prepend(str(ROOT / "tests" / "fixtures"))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            item
            for item in (
                str(ROOT),
                str(ROOT / "tests"),
                os.environ.get("PYTHONPATH", ""),
            )
            if item
        ),
    )
    transports = []

    def forbidden_provider_call(**kwargs):
        transports.append(kwargs)
        raise AssertionError("zero-budget CLI must not enter provider transport")

    monkeypatch.setattr(
        runtime_meter, "_stdlib_https_transport", forbidden_provider_call
    )
    if case == "expired-deadline":
        clock_values = iter((0.0, 1.0))
        monkeypatch.setattr(
            runtime_meter, "_monotonic_clock", lambda: next(clock_values)
        )
    workspace = tmp_path / case
    workspace.mkdir()
    preset = tmp_path / f"preset-{case}"
    shutil.copytree(EXAMPLE, preset)
    config = preset / "run-g0c.toml"
    result = CliRunner().invoke(
        cli,
        [
            "-C",
            str(workspace),
            "run",
            "--config",
            str(config),
            "--wait-timeout",
            "15",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    assert transports == []
    payload = json.loads(result.output)
    root_id = payload["contract_id"]
    store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        records = [record for _, record in store.scan_runtime_records()]
        failure = next(
            record
            for record in records
            if record.record_type == "worker.invocation_failed"
            and record.payload.get("contract_id") == root_id
        )
        expected_error = (
            "provider_deadline_exceeded"
            if case == "expired-deadline"
            else "provider_request_budget_exhausted"
        )
        assert failure.payload["category"] == f"worker_error:{expected_error}"
        contracts = {contract.id: contract for contract in store.get_all_contracts()}
        assert not any(contract.parent_id == root_id for contract in contracts.values())
        assert not any(
            record.record_type == "worker_failure.recovery_scheduled"
            for record in records
        )
        assert not any(
            record.record_type == "lifecycle.terminal"
            and record.payload.get("contract_id") == root_id
            for record in records
        )
        extinguished = [
            record
            for record in records
            if record.record_type == "allowance.extinguished"
            and record.payload.get("contract_id") == root_id
        ]
        assert len(extinguished) == 1
        assert extinguished[0].payload["amount"] == contracts[root_id].budget
    finally:
        store.close()

    runtime_meter.reset_process_session_for_tests()
