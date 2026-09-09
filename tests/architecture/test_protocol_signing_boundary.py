import ast
from pathlib import Path

import aigineering.core.signing as compatibility
import aigineering.protocol.signing as canonical


ROOT = Path(__file__).parents[2]


def test_core_signing_reexports_canonical_protocol_objects():
    assert set(compatibility.__all__) == {
        "DeterministicSigner",
        "DeterministicVerifier",
        "Ed25519Signer",
        "Ed25519Verifier",
        "Signer",
        "Verifier",
        "create_signer",
        "create_verifier",
        "generate_keypair",
    }
    for name in compatibility.__all__:
        assert getattr(compatibility, name) is getattr(canonical, name)


def test_protocol_modules_do_not_import_legacy_core_signing():
    protocol = ROOT / "src/aigineering/protocol"
    for path in protocol.glob("*.py"):
        assert "aigineering.core.signing" not in _imports(path), path


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
