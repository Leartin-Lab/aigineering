from __future__ import annotations

import hashlib
import json

from click.testing import CliRunner

from aigineering.cli.artifact import artifact_group
from aigineering.cli.domain import domain_group


def _json(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_artifact_cli_full_lifecycle_export_and_safety(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        assert runner.invoke(domain_group, ["init", "--json"]).exit_code == 0

        source = tmp_path / "source.txt"
        source.write_text("<script>alert('x')</script> citation text", encoding="utf-8")
        imported = _json(
            runner.invoke(
                artifact_group,
                [
                    "import",
                    str(source),
                    "--name",
                    "source",
                    "--media-type",
                    "text/plain",
                    "--source-uri",
                    "https://example.test/source",
                ],
            )
        )
        source_id = imported["id"]

        parsed = _json(
            runner.invoke(artifact_group, ["parse", source_id, "--name", "document"])
        )
        document_id = parsed["id"]
        shown = _json(runner.invoke(artifact_group, ["show", document_id]))
        assert shown["kind"] == "document"

        cited = _json(
            runner.invoke(
                artifact_group,
                [
                    "cite",
                    document_id,
                    "--name",
                    "citation",
                    "--page",
                    "1",
                    "--start",
                    "0",
                    "--end",
                    "27",
                ],
            )
        )
        evidence_id = cited["id"]

        markdown = tmp_path / "report.md"
        markdown.write_text("Finding[^one]", encoding="utf-8")
        bindings = tmp_path / "bindings.json"
        bindings.write_text(json.dumps({"one": evidence_id}), encoding="utf-8")
        report = _json(
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
        report_id = report["id"]
        lineage = _json(runner.invoke(artifact_group, ["lineage", report_id]))
        assert lineage["root"] == report_id

        output = tmp_path / "export"
        exported = _json(
            runner.invoke(
                artifact_group,
                [
                    "export",
                    report_id,
                    "--output",
                    str(output),
                    "--include-sources",
                ],
            )
        )
        assert exported["report_id"] == report_id
        report_text = (output / "report.md").read_text(encoding="utf-8")
        assert "(evidence/one.html)" in report_text
        assert (output / "evidence/one.html").is_file()
        evidence_html = (output / "evidence/one.html").read_text(encoding="utf-8")
        assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in evidence_html
        assert "<script>alert('x')</script>" not in evidence_html
        assert list((output / "sources").iterdir())

        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        for name, digest in manifest["files"].items():
            assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest

        refused = runner.invoke(
            artifact_group,
            ["export", report_id, "--output", str(output)],
        )
        assert refused.exit_code != 0
        assert "must not already exist" in refused.output

        missing = runner.invoke(artifact_group, ["show", "asset-does-not-exist"])
        assert missing.exit_code != 0
        assert "exact asset ID not found" in missing.output
