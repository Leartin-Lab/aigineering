import ast
from pathlib import Path


_FORBIDDEN_TASK_WRITERS = {
    "CandidatePublisher",
    "commit_local_effects",
    "commit_candidate",
    "commit_ingress_batch",
    "commit_worker_invocation_failure",
    "commit_claim_expiration",
    "commit_replacement_claim",
    "add_contract",
    "add_asset",
    "rebuild_runtime_materializations",
    "rebuild_projection_indexes",
    "rebuild_claim_projection",
    "create_task",
    "publish_task",
    "task_declaration_effect",
    "contract_declaration_effect",
    "execute",
    "executemany",
    "executescript",
}
_FORBIDDEN_WRITER_MODULES = (
    "aigineering.task_publication",
    "aigineering.task_assignment",
    "aigineering.task_priority",
    "aigineering.task_revision",
    "aigineering.task_attachments",
)


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _called_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            names.add(function.id)
        elif isinstance(function, ast.Attribute):
            names.add(function.attr)
    return names


def test_persistent_fleet_delegates_claiming_and_does_not_publish_task_facts():
    root = Path(__file__).parents[2]
    sources = {
        "service": _module(root / "src/aigineering/fleet_service.py"),
        "pull": _module(root / "src/aigineering/local_fleet.py"),
        "preparation": _module(root / "src/aigineering/run_config.py"),
    }

    for tree in sources.values():
        modules = _imported_modules(tree)
        assert not any(
            module == "aigineering.cli" or module.startswith("aigineering.cli.")
            for module in modules
        )
        assert not any(
            module == writer or module.startswith(writer + ".")
            for module in modules
            for writer in _FORBIDDEN_WRITER_MODULES
        )
        assert not (_called_names(tree) & _FORBIDDEN_TASK_WRITERS)

    service_calls = _called_names(sources["service"])
    pull_calls = _called_names(sources["pull"])
    preparation_calls = _called_names(sources["preparation"])

    assert {"run_persistent_local_fleet", "prepare_fleet_resources"} <= service_calls
    assert {
        "claim_next_application_package",
        "execute_captured_package",
        "run_runtime_maintenance_step",
        "sync_runtime",
    } <= pull_calls
    assert "prepare_fleet_resources" in preparation_calls
