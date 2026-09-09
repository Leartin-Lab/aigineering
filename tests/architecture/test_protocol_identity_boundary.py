import ast
from pathlib import Path

import aigineering.core.ids as compatibility
import aigineering.protocol.identity as canonical

ROOT = Path(__file__).parents[2]


def test_core_ids_reexports_canonical_protocol_objects():
    assert compatibility.__all__ == canonical.__all__
    for name in canonical.__all__:
        assert getattr(compatibility, name) is getattr(canonical, name)


def test_protocol_identity_does_not_depend_on_core():
    identity = ROOT / "src/aigineering/protocol/identity.py"
    imports = _imports(identity)
    assert not {name for name in imports if name.startswith("aigineering.core")}


def test_protocol_modules_do_not_import_legacy_core_ids():
    protocol = ROOT / "src/aigineering/protocol"
    for path in protocol.glob("*.py"):
        assert "aigineering.core.ids" not in _imports(path), path


def test_protocol_layer_does_not_import_core():
    protocol = ROOT / "src/aigineering/protocol"
    for path in protocol.glob("*.py"):
        core_imports = {
            name for name in _imports(path) if name.startswith("aigineering.core")
        }
        assert not core_imports, (path, core_imports)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
