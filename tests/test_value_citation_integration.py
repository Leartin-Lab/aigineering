"""Citation CLI and mixed source roles preserve the ordinary Asset boundary."""

from dataclasses import replace
import hashlib
import json

import pytest
from click.testing import CliRunner

from aigineering.business import artifacts
from aigineering.cli import artifact as artifact_cli
from aigineering.cli.artifact import artifact_group
from aigineering.cli.asset import asset_group
from aigineering.cli.domain import domain_group
from aigineering.core.sqlite_store import SQLiteStore


def _json(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_cli_json_citation_export_uses_exact_indexed_closure(tmp_path, monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _json(runner.invoke(domain_group, ["init", "--json"]))
        source = _json(
            runner.invoke(
                asset_group,
                [
                    "add",
                    "--name",
                    "tool-observation",
                    "--content",
                    '{"value":"<script>not executable</script>","other":"SIBLING-SECRET"}',
                    "--json",
                ],
            )
        )
        before = SQLiteStore(".aig/store.db")
        revision = before.get_runtime_revision()
        before.close()
        for args in (["--pointer", "/value", "--page", "1"], [], ["--page", "1"]):
            rejected = runner.invoke(
                artifact_group, ["cite", source["id"], "--name", "bad", *args]
            )
            assert rejected.exit_code != 0
            assert "use --pointer OR all of" in rejected.output
        bad_pointer = runner.invoke(
            artifact_group,
            ["cite", source["id"], "--name", "bad", "--pointer", "/missing"],
        )
        assert bad_pointer.exit_code != 0
        with SQLiteStore(".aig/store.db") as checked:
            assert checked.get_runtime_revision() == revision
        cited = _json(
            runner.invoke(
                artifact_group,
                ["cite", source["id"], "--name", "value", "--pointer", "/value"],
            )
        )
        with SQLiteStore(".aig/store.db") as checked:
            # One signed logical action, not a second follow-up lineage write.
            assert (
                len(
                    checked.scan_runtime_records(
                        after_revision=revision, record_type="candidate.committed"
                    )
                )
                == 1
            )
        markdown, bindings = tmp_path / "report.md", tmp_path / "bindings.json"
        markdown.write_text("Value[^v]", encoding="utf-8")
        bindings.write_text(json.dumps({"v": cited["id"]}), encoding="utf-8")
        rep = _json(
            runner.invoke(
                artifact_group,
                [
                    "report",
                    str(markdown),
                    "--bindings",
                    str(bindings),
                    "--name",
                    "report",
                ],
            )
        )

        def no_scan(*_args, **_kwargs):
            pytest.fail("artifact reads must not scan unrelated history or assets")

        open_store = artifact_cli._persistent_store

        def bounded_reader():
            # Store startup currently rebuilds the graph projection; this test
            # isolates the artifact operation after opening that connection.
            store = open_store()
            monkeypatch.setattr(store, "scan_runtime_records", no_scan)
            monkeypatch.setattr(store, "get_all_assets", no_scan)
            monkeypatch.setattr(store, "get_latest_asset", no_scan)
            return store

        monkeypatch.setattr(artifact_cli, "_persistent_store", bounded_reader)
        graph = _json(runner.invoke(artifact_group, ["lineage", rep["id"]]))
        assert {x["asset_id"] for x in graph["publications"]} == {
            source["id"],
            cited["id"],
            rep["id"],
        }
        assert graph["unresolved_runtime_parents"] == []
        assert all("effects" not in record for record in graph["runtime_ancestry"])
        output = tmp_path / "export"
        _json(
            runner.invoke(
                artifact_group, ["export", rep["id"], "--output", str(output)]
            )
        )
        rendered = (output / "evidence/v.html").read_text()
        assert "&lt;script&gt;not executable&lt;/script&gt;" in rendered
        assert "<script>" not in rendered
        assert "SIBLING-SECRET" not in "".join(
            p.read_text() for p in output.rglob("*") if p.is_file()
        )
        manifest = json.loads((output / "manifest.json").read_text())
        for name, digest in manifest["files"].items():
            assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("raw_first", [True, False])
@pytest.mark.parametrize("forge_document", [True, False])
def test_raw_source_role_cannot_bypass_artifact_validation(raw_first, forge_document):
    source = artifacts.attachment("file", b"raw", media_type="text/plain")
    doc = artifacts.document(
        "doc",
        source,
        {
            "schema": "document-extraction-v1",
            "tool": "fixture",
            "version": "1",
            "ocr": False,
            "pages": [{"page": 1, "text": "value"}],
        },
    )
    # Canonical Worker /exec carries artifact envelopes in text Assets.
    doc = replace(doc, content_type="text")
    text = artifacts.evidence("text", doc, page=1, start=0, end=5)
    raw = artifacts.json_evidence("json", doc, "/payload/pages/0/text")
    # Pure consumer tests control IDs solely to force both traversal orders.
    raw = replace(raw, id="a" if raw_first else "z")
    text = replace(text, id="z" if raw_first else "a")
    rep = artifacts.report(
        "report", "A[^raw] B[^text]", {"raw": raw.id, "text": text.id}
    )
    if forge_document:
        value = artifacts.envelope(doc)
        value["parents"] = ["missing-attachment"]
        doc = replace(doc, content=json.dumps(value))
        # Recompute raw quote after tampering: rejection must come from the
        # artifact role, not a stale JSON digest failing before that role.
        raw = replace(
            artifacts.json_evidence("json", doc, "/payload/pages/0/text"), id=raw.id
        )
    assets = {x.id: x for x in (source, doc, text, raw, rep)}
    if forge_document:
        with pytest.raises(ValueError, match="missing or not visible"):
            artifacts.lineage(rep.id, assets.get)
    else:
        graph = artifacts.lineage(rep.id, assets.get)
        assert (
            next(n for n in graph["nodes"] if n["id"] == doc.id)["kind"] == "document"
        )


@pytest.mark.parametrize(
    "number", ["-0", "1e999999999999999999999999", "9007199254740993"]
)
def test_number_lexemes_do_not_expand_or_round(number):
    from aigineering.core.control_plane import build_control_plane_asset

    source = build_control_plane_asset(name="n", content=number)
    assert (
        artifacts.envelope(artifacts.json_evidence("v", source, ""))["payload"]["quote"]
        == number
    )


@pytest.mark.parametrize(
    "text,pointer",
    [
        ('{"a":1,"\\u0061":2}', "/a"),
        ('{"x":1}', "/~2"),
        ("[1]", "/00"),
        ("[1]", "/-1"),
        ("[1]", "/" + "9" * 5000),
    ],
)
def test_ambiguous_json_or_invalid_pointer_fails_closed(text, pointer):
    from aigineering.core.control_plane import build_control_plane_asset

    with pytest.raises(ValueError):
        artifacts.json_evidence(
            "bad", build_control_plane_asset(name="n", content=text), pointer
        )


def test_cli_empty_pointer_selects_scalar_root(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _json(runner.invoke(domain_group, ["init", "--json"]))
        source = _json(
            runner.invoke(
                asset_group, ["add", "--name", "root", "--content", "17", "--json"]
            )
        )
        cited = _json(
            runner.invoke(
                artifact_group,
                ["cite", source["id"], "--name", "root-value", "--pointer", ""],
            )
        )
        shown = _json(runner.invoke(artifact_group, ["show", cited["id"]]))
        assert shown["payload"]["quote"] == "17"
        assert shown["payload"]["pointer"] == ""


def test_json_evidence_digest_and_scalar_type_cannot_be_forged():
    from aigineering.core.control_plane import build_control_plane_asset

    source = build_control_plane_asset(name="n", content='{"x":1}')
    cited = artifacts.json_evidence("v", source, "/x")
    for field, changed in (("source_sha256", "0" * 64), ("value_type", "string")):
        value = artifacts.envelope(cited)
        value["payload"][field] = changed
        forged = replace(cited, content=json.dumps(value))
        with pytest.raises(ValueError, match="exact source"):
            artifacts.lineage(forged.id, {source.id: source, forged.id: forged}.get)
