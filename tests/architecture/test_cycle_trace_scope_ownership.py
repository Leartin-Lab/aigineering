"""Cycle attribution shares one bounded, read-only application implementation."""

import ast
from pathlib import Path

from aigineering.cli import run
from aigineering import local_fleet


def test_cli_and_fleet_share_cycle_trace_attribution():
    assert run._cycle_trace_scope is local_fleet._cycle_trace_scope
    cli_tree = ast.parse(Path(run.__file__).read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.FunctionDef) and node.name == "_cycle_trace_scope"
        for node in ast.walk(cli_tree)
    )


def test_cycle_trace_attribution_only_reads_its_supplied_window():
    tree = ast.parse(Path(local_fleet.__file__).read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_cycle_trace_scope"
    )
    calls = {
        ast.unparse(node.func)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    }
    assert calls == {
        "set",
        "runtime_record_contract_references",
        "contracts.update",
        "record.payload.get",
        "isinstance",
        "trace.get",
        "unassigned.append",
        "sorted",
        "len",
    }
