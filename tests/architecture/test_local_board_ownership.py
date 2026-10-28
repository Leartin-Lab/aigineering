"""Named boards remain local configuration, separate from task authority."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _tree(path: str) -> ast.Module:
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def _imports(tree: ast.Module) -> set[str]:
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    names.update(
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    )
    return names


def test_board_registry_is_not_a_core_or_signed_task_authority():
    board = _tree("src/aigineering/local_boards.py")
    imports = _imports(board)
    assert not any(name.startswith("aigineering.cli") for name in imports)
    assert not any(
        name.startswith(
            (
                "aigineering.plugins",
                "aigineering.protocol.effect_builders",
                "aigineering.core.candidate_publisher",
                "aigineering.task_publication",
            )
        )
        for name in imports
    )
    core_imports = set()
    for path in (ROOT / "src/aigineering/core").rglob("*.py"):
        core_imports.update(_imports(ast.parse(path.read_text(encoding="utf-8"))))
    assert not any(
        "local_boards" in name or "cli.board" in name for name in core_imports
    )
    calls = {
        node.func.attr
        for node in ast.walk(board)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not calls.intersection(
        {
            "append_runtime_record",
            "commit_ingress_batch",
            "commit_candidate",
            "claim_contract",
            "publish_task_contract",
        }
    )


def test_domain_init_and_board_creation_share_explicit_bootstrap_owner():
    domain_cli = _tree("src/aigineering/cli/domain.py")
    board_app = _tree("src/aigineering/local_boards.py")
    domain_calls = {
        node.func.id
        for node in ast.walk(domain_cli)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    board_calls = {
        node.func.id
        for node in ast.walk(board_app)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "initialize_local_domain" in domain_calls
    assert "initialize_local_domain" in board_calls


def test_global_board_routing_and_management_commands_are_registered():
    main = _tree("src/aigineering/cli/main.py")
    source = (ROOT / "src/aigineering/cli/main.py").read_text(encoding="utf-8")
    assert '"--board"' in source
    assert "board_group" in source
    assert "resolve_board_workspace" in source
    assert "ctx.call_on_close" in source
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_command"
        for node in ast.walk(main)
    )
