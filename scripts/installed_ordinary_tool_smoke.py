"""Installed-wheel ordinary signed tool and continuation path."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import uuid


def ordinary_tool_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Run one fixture tool action and its ordinary output continuation."""
    import subprocess
    import uuid

    from installed_smoke import json_command

    workspace = cwd / f"ordinary-tool-{uuid.uuid4().hex}"
    workspace.mkdir(mode=0o700)
    fixtures = workspace / "fixtures"
    fixtures.mkdir(mode=0o700)
    worker_module = fixtures / "fixture_worker.py"
    worker_module.write_text(
        "import json\n"
        "from aigineering.core.worker_routing import WorkerRegistration\n"
        "from aigineering.protocol.types import Candidate\n"
        "class Reasoner:\n"
        " def __init__(self, worker_id): self.worker_id=worker_id\n"
        " def registration(self): return WorkerRegistration(self.worker_id, "
        "capabilities=('installed.reasoner',))\n"
        " def invoke(self, contract, disclosed_assets):\n"
        "  if contract.origin == 'continuation':\n"
        "   observation=next(a for a in disclosed_assets if a.name.startswith('tool_observation_'))\n"
        "   observed=json.loads(observation.content)\n"
        "   assert observed.get('result') == 'fixture lookup: x'\n"
        "   raw='/exec '+json.dumps({'outputs': {contract.outputs[0]: observed['result']}})\n"
        "  else: raw='/tool '+json.dumps({'name':'lookup','args':{'key':'x'}})\n"
        "  return Candidate(worker_id=self.worker_id, raw_output=raw)\n"
        "def build_worker(*, worker_id): return Reasoner(worker_id)\n"
        "def build_registry():\n"
        " from aigineering.core.tools import ToolRegistry\n"
        " from aigineering.protocol.types import ToolSpec\n"
        " registry=ToolRegistry()\n"
        " registry.register(ToolSpec(name='lookup', "
        "description='Deterministic installed smoke lookup', "
        "input_schema={'type':'object'}), "
        "lambda args: 'fixture lookup: '+args['key'])\n"
        " return registry\n",
        encoding="utf-8",
    )
    (workspace / "fleet.toml").write_text(
        '[fleet]\ndb_path=".aig/store.db"\npoll_interval=0.02\n'
        '[[workers]]\nid="installed-reasoner"\nkind="local"\n'
        'worker_factory="fixture_worker:build_worker"\n'
        'capabilities=["installed.reasoner"]\n'
        '[[workers]]\nid="installed-tools"\nkind="tool"\n'
        'tool_registry="fixture_worker:build_registry"\n',
        encoding="utf-8",
    )
    fixture_env = dict(env)
    fixture_env["PYTHONPATH"] = str(fixtures)
    initialized = json_command(
        aig, ["-C", str(workspace), "domain", "init"], cwd=cwd, env=fixture_env
    )
    if not initialized.get("domain_id"):
        raise RuntimeError("tool fixture domain initialization failed")
    input_asset = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "asset",
            "add",
            "--name",
            "tool-source",
            "--content",
            "frozen tool input",
        ],
        cwd=cwd,
        env=fixture_env,
    )
    if not input_asset.get("id"):
        raise RuntimeError("tool fixture input Asset was not published")
    task = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "create",
            "--name",
            "installed-tool-continuation",
            "--input",
            "tool-source",
            "--output",
            "report",
            "--tool",
            "lookup",
            "--requires-capability",
            "installed.reasoner",
            "--budget",
            "5",
        ],
        cwd=cwd,
        env=fixture_env,
    )
    contract_id = task.get("contract_id")
    if not contract_id:
        raise RuntimeError("tool fixture Task was not declared")
    run = subprocess.run(
        [
            str(aig),
            "-C",
            str(workspace),
            "fleet",
            "run",
            "--config",
            str(workspace / "fleet.toml"),
            "--task",
            contract_id,
            "--wait-timeout",
            "45",
            "--json",
        ],
        cwd=cwd,
        env=fixture_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if run.returncode != 0:
        raise RuntimeError(f"installed ordinary tool workflow failed: {run.stderr}")
    payload = json.loads(run.stdout)
    if payload.get("status") != "complete":
        raise RuntimeError(f"tool workflow did not complete: {payload}")
    status = json_command(
        aig,
        ["-C", str(workspace), "task", "status", contract_id],
        cwd=cwd,
        env=fixture_env,
    )
    if status.get("status") != "completed":
        raise RuntimeError(f"tool workflow did not satisfy its Task: {status}")
    records = json.loads(
        subprocess.run(
            [
                str(python),
                "-c",
                "import json,sys; from aigineering.core.sqlite_store import SQLiteStore; "
                "from aigineering.protocol.runtime_record import runtime_record_effective_payload; "
                "s=SQLiteStore(sys.argv[1]); rows=[r for _,r in s.scan_runtime_records()]; "
                "print(json.dumps([{'id':r.id,'type':r.record_type,'payload':runtime_record_effective_payload(r)['payload']} for r in rows])); s.close()",
                str(workspace / ".aig/store.db"),
            ],
            cwd=cwd,
            env=fixture_env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
    )
    task_declarations = [
        item
        for item in records
        if item["type"] == "contract.declared"
        and item["payload"].get("contract", {}).get("parent_id") == contract_id
    ]
    observation = any(
        item["type"] == "asset.committed"
        and item["payload"]
        .get("asset", {})
        .get("name", "")
        .startswith("tool_observation_")
        for item in records
    )
    declaration_candidate_ids = {
        item["payload"].get("candidate_id") for item in task_declarations
    }
    if (
        len(task_declarations) != 2
        or len(declaration_candidate_ids) != 1
        or None in declaration_candidate_ids
        or not observation
    ):
        raise RuntimeError("tool receipt/continuation facts were not both committed")
    method_contract_ids = {
        item["payload"].get("contract", {}).get("id") for item in task_declarations
    }
    observed_assets = [
        item["payload"].get("asset", {})
        for item in records
        if item["type"] == "asset.committed"
        and item["payload"]
        .get("asset", {})
        .get("name", "")
        .startswith("tool_observation_")
    ]
    if (
        not observed_assets
        or observed_assets[0].get("created_by") not in method_contract_ids
    ):
        raise RuntimeError("tool observation lacks its committed method source")
    heads = [
        item
        for item in records
        if item.get("type") == "candidate.committed"
        and item.get("payload", {}).get("candidate_id") in declaration_candidate_ids
    ]
    if len(heads) != 1 or not {item["id"] for item in task_declarations} <= set(
        heads[0]["payload"]["committed_record_ids"]
    ):
        raise RuntimeError("tool declarations lack their exact shared committed head")
    descriptors = {
        item["payload"]["asset"]["id"]
        for item in records
        if item["type"] == "asset.committed"
        and item["payload"].get("asset", {}).get("name") == "_tool_capability_lookup"
    }
    if len(descriptors) != 1:
        raise RuntimeError("Fleet did not publish one unambiguous tool descriptor")
    return {
        "contract_id": contract_id,
        "input_asset_id": input_asset["id"],
        "tool_descriptor_asset_id": next(iter(descriptors)),
        "continuation_declarations": len(task_declarations),
        "tool_observation_committed": observation,
        "status": status["status"],
        "provider_calls": 0,
    }


def planning_declaration_smoke(
    aig: Path, python: Path, *, cwd: Path, env: dict[str, str]
) -> dict:
    """Verify `/plan` commits its staged ordinary Contracts as one Candidate."""
    import subprocess
    import uuid

    from installed_smoke import json_command

    workspace = cwd / f"ordinary-plan-{uuid.uuid4().hex}"
    workspace.mkdir(mode=0o700)
    initialized = json_command(
        aig, ["-C", str(workspace), "domain", "init"], cwd=cwd, env=env
    )
    if not initialized.get("domain_id"):
        raise RuntimeError("planning fixture domain initialization failed")
    created = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "create",
            "--name",
            "installed-plan-stages",
            "--description",
            "Plan a fixture task through the ordinary staged planning action.",
            "--output",
            "final_report",
            "--budget",
            "8",
        ],
        cwd=cwd,
        env=env,
    )
    contract_id = created.get("contract_id")
    if not contract_id:
        raise RuntimeError("plan fixture Task was not declared")
    run_result = subprocess.run(
        [
            str(aig),
            "-C",
            str(workspace),
            "run",
            "--once",
            "--worker",
            "mock",
            "--mock-output",
            '/plan {"reason":"fixture staged planning"}',
            "--json",
        ],
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if run_result.returncode != 1:
        raise RuntimeError(
            f"one-cycle planning did not return its expected incomplete result: {run_result.stdout}"
        )
    run = json.loads(run_result.stdout)
    if run.get("submission_status") != "task_delegated":
        raise RuntimeError(f"signed plan action did not delegate: {run}")
    facts = json.loads(
        subprocess.run(
            [
                str(python),
                "-c",
                "import json,sys; from aigineering.core.sqlite_store import SQLiteStore; "
                "from aigineering.protocol.runtime_record import runtime_record_effective_payload; "
                "s=SQLiteStore(sys.argv[1]); print(json.dumps([{'type':r.record_type,"
                "'payload':runtime_record_effective_payload(r)['payload']} for _,r in s.scan_runtime_records()])); s.close()",
                str(workspace / ".aig/store.db"),
            ],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
    )
    staged = [
        item
        for item in facts
        if item["type"] == "contract.declared"
        and item["payload"].get("contract", {}).get("parent_id") == contract_id
    ]
    expected = {"plugin:plan.draft", "plugin:plan.dependencies", "plugin:plan.compile"}
    labels = {
        label
        for item in staged
        for label in item["payload"].get("contract", {}).get("labels", ())
    }
    candidate_ids = {item["payload"].get("candidate_id") for item in staged}
    if len(staged) != 3 or not expected <= labels or len(candidate_ids) != 1:
        raise RuntimeError("planning stages were not published atomically together")
    if None in candidate_ids or not any(
        item["type"] == "candidate.committed"
        and item["payload"].get("candidate_id") in candidate_ids
        for item in facts
    ):
        raise RuntimeError("planning stage declarations lack a signed commit head")
    return {
        "contract_id": contract_id,
        "stage_count": len(staged),
        "stages": sorted(expected),
        "single_signed_candidate": True,
        "status": "task_delegated",
        "provider_calls": 0,
    }


def planning_completion_smoke(
    aig: Path,
    python: Path,
    source_root: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict:
    """Run the checked-in research plan through ordinary Fleet Task execution."""
    from installed_smoke import json_command

    workspace = cwd / f"installed-planning-completion-{uuid.uuid4().hex}"
    workspace.mkdir(mode=0o700)
    fixture_root = workspace / "fixture-code"
    fixture_package = fixture_root / "fixtures"
    fixture_package.mkdir(parents=True)
    (fixture_package / "__init__.py").write_text("", encoding="utf-8")
    worker_source = source_root / "tests" / "fixtures" / "cli_research_workers.py"
    worker_bytes = worker_source.read_bytes()
    (fixture_package / "cli_research_workers.py").write_bytes(worker_bytes)
    example_package = fixture_root / "examples"
    research_example = example_package / "cli_research"
    research_example.mkdir(parents=True)
    (example_package / "__init__.py").write_text("", encoding="utf-8")
    for name in (
        "__init__.py",
        "tools.py",
        "run.toml",
        "sample.json",
        "task-description.txt",
        "skill.toml",
        "SKILL.md",
    ):
        shutil.copyfile(
            source_root / "examples" / "cli_research" / name, research_example / name
        )

    workers = research_example / "workers.toml"
    workers.write_text(
        '[fleet]\ndb_path=".aig/store.db"\npoll_interval=0.02\n'
        '[[workers]]\nid="installed-planner"\nkind="local"\n'
        'worker_factory="fixtures.cli_research_workers:build_research_worker"\n'
        'capabilities=["measurement.plan"]\npools=["planning"]\n'
        '[[workers]]\nid="installed-researcher"\nkind="local"\n'
        'worker_factory="fixtures.cli_research_workers:build_research_worker"\n'
        'capabilities=["measurement.research"]\npools=["research"]\n'
        '[[workers]]\nid="installed-tools"\nkind="tool"\n'
        'tool_registry="examples.cli_research.tools:build_registry"\n'
        '[[workers]]\nid="installed-verifier"\nkind="local"\n'
        'worker_factory="fixtures.cli_research_workers:build_verifier_worker"\n'
        'capabilities=["measurement.verify"]\npools=["verification"]\n'
        'effect_capabilities=["asset.attest", "measurement.verify"]\n',
        encoding="utf-8",
    )
    fixture_env = dict(env)
    fixture_env["PYTHONPATH"] = str(fixture_root)
    run = subprocess.run(
        [
            str(aig),
            "-C",
            str(workspace),
            "run",
            "--config",
            str(research_example / "run.toml"),
            "--wait-timeout",
            "90",
            "--json",
        ],
        cwd=cwd,
        env=fixture_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if run.returncode != 0:
        raise RuntimeError(
            "installed planning fixture failed "
            f"(returncode={run.returncode}) stdout={run.stdout!r} stderr={run.stderr!r}"
        )
    payload = json.loads(run.stdout)
    root_id = payload.get("contract_id")
    if payload.get("completed") is not True or payload.get("status") != "complete":
        raise RuntimeError(f"installed planning fixture did not complete: {payload}")

    report_path = workspace / "report.json"
    exported = json_command(
        aig,
        [
            "-C",
            str(workspace),
            "task",
            "result",
            root_id,
            "--output",
            "measurement_report",
            "--to",
            str(report_path),
        ],
        cwd=cwd,
        env=fixture_env,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (
        exported.get("data", {}).get("status") != "completed"
        or report.get("count") != 4
        or report.get("mean_c") != 15.0
        or report.get("mean_f") != 59.0
        or len(report.get("observation_asset_ids", [])) != 2
    ):
        raise RuntimeError("installed planned result did not match fixture evidence")

    facts = json.loads(
        subprocess.run(
            [
                str(python),
                "-c",
                "import json,sys; from aigineering.core.sqlite_store import SQLiteStore; "
                "from aigineering.protocol.immutability import deep_thaw; "
                "s=SQLiteStore(sys.argv[1]); rows=s.scan_runtime_records(); "
                "print(json.dumps({'records':[{'type':r.record_type,'payload':deep_thaw(r.payload)} for _,r in rows],"
                "'contracts':[{'id':c.id,'name':c.name,'parent_id':c.parent_id,'labels':list(c.labels)} for c in s.get_all_contracts()]},sort_keys=True)); s.close()",
                str(workspace / ".aig" / "store.db"),
            ],
            cwd=cwd,
            env=fixture_env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
    )
    contracts = facts["contracts"]
    direct = [item for item in contracts if item["parent_id"] == root_id]
    labels = {label for item in direct for label in item["labels"]}
    if not {
        "plugin:plan.draft",
        "plugin:plan.dependencies",
        "plugin:plan.compile",
    } <= labels or not any(
        item["name"] == "measurement_research" for item in contracts
    ):
        raise RuntimeError("installed plan did not compile its ordinary child Tasks")
    observations = [
        item["payload"].get("asset", {})
        for item in facts["records"]
        if item["type"] == "asset.committed"
        and item["payload"]
        .get("asset", {})
        .get("name", "")
        .startswith("tool_observation_")
    ]
    if len(observations) != 2:
        raise RuntimeError(
            "installed planned children did not commit both tool observations"
        )
    before_rebuild = json.loads(
        subprocess.run(
            [
                str(python),
                "-c",
                "import json,sys; from aigineering.core.sqlite_store import SQLiteStore; s=SQLiteStore(sys.argv[1]); print(json.dumps([r.id for _,r in s.scan_runtime_records()])); s.close()",
                str(workspace / ".aig" / "store.db"),
            ],
            cwd=cwd,
            env=fixture_env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
    )
    json_command(
        aig, ["-C", str(workspace), "redis", "rebuild"], cwd=cwd, env=fixture_env
    )
    after_rebuild = json.loads(
        subprocess.run(
            [
                str(python),
                "-c",
                "import json,sys; from aigineering.core.sqlite_store import SQLiteStore; s=SQLiteStore(sys.argv[1]); print(json.dumps([r.id for _,r in s.scan_runtime_records()])); s.close()",
                str(workspace / ".aig" / "store.db"),
            ],
            cwd=cwd,
            env=fixture_env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
    )
    if before_rebuild != after_rebuild:
        raise RuntimeError("installed planning reopen/rebuild changed runtime facts")

    return {
        "root_contract_id": root_id,
        "status": "completed",
        "ordinary_planning_children": True,
        "tool_observations": len(observations),
        "exact_fixture_result_export": True,
        "rebuild_preserved_record_ids": True,
        "fixture_worker_sha256": hashlib.sha256(worker_bytes).hexdigest(),
        "provider_calls": 0,
    }
