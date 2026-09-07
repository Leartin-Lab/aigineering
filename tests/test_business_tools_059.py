from __future__ import annotations

import json

import pytest
from conftest import candidate_runtime

from aigineering.business.tools import build_registry
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.tool_schema import ToolSchemaValidationError


def test_business_registry_uses_explicit_asset_snapshot_and_validates_tools(
    tmp_path, monkeypatch
):
    store = SQLiteStore(":memory:")
    runtime, root, document, evidence, report = _chain(store)
    registry = build_registry(assets=(root, document, evidence, report))

    extraction = json.loads(registry.run("document_extract", {"asset_id": root.id}))
    assert extraction["schema"] == "document-extraction-v1"
    assert extraction["pages"][0]["text"] == "raw bytes"

    # The handler only has its tuple snapshot. A fresh working directory and
    # inaccessible ambient store do not affect an already-built registry.
    monkeypatch.chdir(tmp_path)
    assert (
        json.loads(registry.run("document_extract", {"asset_id": root.id}))
        == extraction
    )

    with pytest.raises(ValueError, match="outside configured tool scope"):
        registry.run("document_extract", {"asset_id": "asset:unknown"})
    with pytest.raises(ToolSchemaValidationError):
        registry.run("document_extract", {"asset_id": root.id, "unexpected": True})

    verified = json.loads(registry.run("artifact_verify", {"asset_id": report.id}))
    assert verified["root"] == report.id
    assert {node["id"] for node in verified["nodes"]} == {
        root.id,
        document.id,
        evidence.id,
        report.id,
    }

    incomplete = build_registry(assets=(document, evidence, report))
    with pytest.raises(ValueError, match="missing"):
        incomplete.run("artifact_verify", {"asset_id": report.id})
    store.close()


def test_business_registry_fulltext_output_matches_declared_schema():
    registry = build_registry(assets=())
    output = json.loads(
        registry.run(
            "fulltext_locations",
            {
                "provider": "unpaywall",
                "record": {
                    "doi": "10.1234/example",
                    "best_oa_location": {
                        "url_for_pdf": "https://repo.example/article.pdf",
                        "version": "publishedVersion",
                        "license": "cc-by",
                    },
                },
            },
        )
    )
    assert len(output) == 1
    assert output[0] == {
        "url": "https://repo.example/article.pdf",
        "format": "pdf",
        "version": "publishedVersion",
        "license": "cc-by",
        "provider": "unpaywall",
        "access": "open",
        "original_work": "10.1234/example",
    }


def _chain(store):
    from aigineering.business.artifacts import attachment, document, evidence, report
    from aigineering.protocol.effect_builders import asset_proposal_effect

    runtime = candidate_runtime(store)

    def publish(asset, key):
        decision = runtime.publisher.publish(
            (asset_proposal_effect(asset),), idempotency_key=key
        )
        assert decision.accepted, decision.runtime_records
        return decision.assets[0]

    root = publish(
        attachment("source", b"raw bytes", media_type="text/plain"),
        "business-tools-attachment",
    )
    doc = publish(
        document(
            "source-document",
            root,
            {
                "schema": "document-extraction-v1",
                "pages": [{"page": 1, "text": "raw bytes"}],
                "tool": "test",
                "version": "1",
                "ocr": False,
            },
        ),
        "business-tools-document",
    )
    ev = publish(
        evidence("citation", doc, page=1, start=0, end=4),
        "business-tools-evidence",
    )
    rep = publish(
        report("report", "Finding[^one]", {"one": ev.id}),
        "business-tools-report",
    )
    return runtime, root, doc, ev, rep
