"""Read-only, deterministic rendering of exact artifact citation bindings."""

from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import tempfile
from pathlib import Path

from aigineering.business.artifacts import attachment_bytes, envelope, lineage


def _key(asset_id: str) -> str:
    return hashlib.sha256(asset_id.encode("utf-8")).hexdigest()


def render_bundle(
    report_id, get_asset, *, include_sources: bool = False, records=()
) -> dict[str, bytes]:
    """Prepare an offline bundle without writing or widening disclosure.

    The lookup must enforce the caller's visibility. Original file redistribution
    is an explicit option; a citation is not a license grant. HTML only displays
    escaped source text; arbitrary report Markdown is never executed as HTML.
    """
    graph = lineage(report_id, get_asset, records=records)
    root = envelope(get_asset(report_id))
    if root["kind"] != "report":
        raise ValueError("export requires a report artifact")
    files: dict[str, bytes] = {}
    definitions = []
    for anchor, evidence_id in sorted(root["payload"]["citations"].items()):
        ev = envelope(get_asset(evidence_id))
        doc_id = ev["parents"][0]
        doc = envelope(get_asset(doc_id))
        source_id = doc["parents"][0]
        source = get_asset(source_id)
        source_meta = envelope(source)["payload"]
        p = ev["payload"]
        page_text = doc["payload"]["pages"][p["page"] - 1]["text"]
        marked = (
            html.escape(page_text[: p["start"]])
            + "<mark>"
            + html.escape(p["quote"])
            + "</mark>"
            + html.escape(page_text[p["end"] :])
        )
        source_link = "Original file omitted; consult source metadata."
        if include_sources:
            extension = (
                "pdf" if source_meta["media_type"] == "application/pdf" else "bin"
            )
            filename = f"sources/{_key(source_id)}.{extension}"
            files[filename] = attachment_bytes(source)
            source_link = (
                f'<a href="../{filename}#page={p["page"]}">Original file, '
                f"page {p['page']}</a>"
            )
        metadata = {
            "evidence_asset_id": evidence_id,
            "document_asset_id": doc_id,
            "source_asset_id": source_id,
            "source_sha256": source_meta["sha256"],
            "source_uri": source_meta["source_uri"],
            "license": source_meta["license"],
            "retrieved_at": source_meta["retrieved_at"],
            "acquisition": source_meta.get("acquisition"),
            "tool": doc["payload"]["tool"],
            "version": doc["payload"]["version"],
            "ocr": doc["payload"]["ocr"],
            "selector": p,
            "semantic_support": "not_evaluated",
        }
        page = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
            "style-src 'unsafe-inline'\"><title>Evidence</title>"
            "<style>body{max-width:70em;margin:2em auto;padding:1em}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body>"
            f"<h1>Evidence {html.escape(anchor)}</h1><p>{source_link}</p>"
            f"<pre>{marked}</pre><h2>Provenance</h2>"
            f"<pre>{html.escape(json.dumps(metadata, ensure_ascii=False, indent=2))}</pre>"
            '<p><a href="../lineage.json">Full ancestry graph</a></p></body></html>'
        )
        filename = f"evidence/{anchor}.html"
        files[filename] = page.encode("utf-8")
        definitions.append(f"[^{anchor}]: [Evidence, page {p['page']}]({filename})")
    files["report.md"] = (
        root["payload"]["markdown"] + "\n\n" + "\n".join(definitions) + "\n"
    ).encode("utf-8")
    files["lineage.json"] = json.dumps(
        graph, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8")
    files["citations.json"] = json.dumps(
        {"report_asset_id": report_id, "citations": root["payload"]["citations"]},
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    files["manifest.json"] = json.dumps(
        {
            "schema": "artifact-export-v1",
            "root": report_id,
            "includes_sources": include_sources,
            "files": {
                name: hashlib.sha256(data).hexdigest()
                for name, data in sorted(files.items())
            },
        },
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    return files


def export_bundle(
    report_id, get_asset, output: Path, *, include_sources: bool = False, records=()
) -> None:
    """Write a new private directory after all references have validated."""
    files = render_bundle(
        report_id, get_asset, include_sources=include_sources, records=records
    )
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("export directory must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the final directory exclusively; an interrupted export never
    # overwrites someone else's work. Build in a private sibling first.
    staging = Path(tempfile.mkdtemp(prefix=".aig-export-", dir=output.parent))
    reserved = False
    try:
        for name, data in files.items():
            path = staging / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open("xb") as stream:
                os.chmod(path, 0o600)
                stream.write(data)
        output.mkdir(mode=0o700)
        reserved = True
        # Rename over our own empty directory, never a preexisting user path.
        staging.replace(output)
        reserved = False
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if reserved:
            output.rmdir()
