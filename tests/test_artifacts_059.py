from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest
from conftest import candidate_runtime

from aigineering.business.artifacts import (
    attachment,
    attachment_bytes,
    document,
    envelope,
    evidence,
    lineage,
    report,
)
from aigineering.business.export import render_bundle
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.diagnostics import verify_reconstruction
from aigineering.protocol.effect_builders import asset_proposal_effect


def _publish(runtime, asset, key: str):
    decision = runtime.publisher.publish(
        (asset_proposal_effect(asset),), idempotency_key=key
    )
    assert decision.accepted, decision.runtime_records
    assert len(decision.assets) == 1
    return decision.assets[0]


def _chain(store):
    runtime = candidate_runtime(store)
    root = _publish(
        runtime,
        attachment("source", b"raw bytes", media_type="application/pdf"),
        "artifact-attachment",
    )
    doc = _publish(
        runtime,
        document(
            "source-document",
            root,
            {
                "schema": "document-extraction-v1",
                "pages": [{"page": 1, "text": "alpha beta"}],
                "tool": "test",
                "version": "1",
                "ocr": False,
            },
        ),
        "artifact-document",
    )
    ev = _publish(
        runtime, evidence("citation", doc, page=1, start=0, end=5), "artifact-evidence"
    )
    rep = _publish(
        runtime, report("report", "Finding[^one]", {"one": ev.id}), "artifact-report"
    )
    return runtime, root, doc, ev, rep


def test_attachment_preserves_bytes_and_detects_tampering() -> None:
    store = SQLiteStore(":memory:")
    _runtime, root, _doc, _ev, _rep = _chain(store)
    assert attachment_bytes(root) == b"raw bytes"
    value = envelope(root)
    forged = replace(
        root,
        content=root.content.replace(
            value["payload"]["data"], "x" + value["payload"]["data"], 1
        ),
    )
    with pytest.raises(ValueError, match="artifact-v1 envelope|integrity|base64"):
        attachment_bytes(forged)
    store.close()


def test_document_evidence_report_lineage_uses_exact_ids() -> None:
    store = SQLiteStore(":memory:")
    _runtime, root, doc, ev, rep = _chain(store)
    assets = {asset.id: asset for asset in (root, doc, ev, rep)}
    result = lineage(rep.id, assets.get)
    assert result["root"] == rep.id
    assert [node["id"] for node in result["nodes"]] == sorted(assets)
    assert envelope(rep)["parents"] == [ev.id]
    store.close()


def test_same_name_assets_have_distinct_stable_versions() -> None:
    store = SQLiteStore(":memory:")
    runtime = candidate_runtime(store)
    first = _publish(
        runtime, attachment("same", b"one", media_type="text/plain"), "same-1"
    )
    second = _publish(
        runtime, attachment("same", b"two", media_type="text/plain"), "same-2"
    )
    assert first.id != second.id
    assert [item.id for item in store.get_assets_by_name("same")] == [
        first.id,
        second.id,
    ]
    store.close()


def test_missing_parent_and_closed_disclosure_fail_closed() -> None:
    store = SQLiteStore(":memory:")
    _runtime, root, doc, ev, rep = _chain(store)
    assets = {asset.id: asset for asset in (root, doc, ev, rep)}
    with pytest.raises(ValueError, match="missing|visible"):
        lineage(
            rep.id,
            lambda asset_id: None if asset_id == doc.id else assets.get(asset_id),
        )
    sealed = replace(doc, disclosure_view="sealed")
    assets[doc.id] = sealed
    with pytest.raises(ValueError, match="available for disclosure"):
        lineage(rep.id, assets.get)
    store.close()


def test_forged_quote_and_report_binding_are_rejected() -> None:
    store = SQLiteStore(":memory:")
    _runtime, root, doc, ev, rep = _chain(store)
    value = envelope(ev)
    forged = replace(
        ev,
        content=ev.content.replace(value["payload"]["quote"], "omega", 1),
    )
    assets = {asset.id: asset for asset in (root, doc, forged, rep)}
    with pytest.raises(ValueError, match="exact source range"):
        lineage(rep.id, assets.get)
    with pytest.raises(ValueError, match="match exactly"):
        report("bad", "Finding[^one]", {"two": ev.id})
    forged_report_value = envelope(rep)
    forged_report_payload = dict(forged_report_value["payload"])
    forged_report_payload["citations"] = {"one": "wrong-id"}
    forged_report_content = json.dumps(
        {
            "schema": forged_report_value["schema"],
            "kind": forged_report_value["kind"],
            "parents": forged_report_value["parents"],
            "payload": forged_report_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    forged_report = replace(rep, content=forged_report_content)
    with pytest.raises(ValueError, match="exact evidence parents"):
        lineage(
            rep.id, {root.id: root, doc.id: doc, ev.id: ev, rep.id: forged_report}.get
        )
    store.close()


def test_artifact_inputs_fail_closed_for_types_and_unsafe_bindings() -> None:
    store = SQLiteStore(":memory:")
    _runtime, root, doc, _ev, _rep = _chain(store)
    with pytest.raises(ValueError):
        evidence("bool-page", doc, page=True, start=0, end=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="anchor"):
        report("unsafe", "Finding[^../secret]", {"../secret": "asset:v1:x"})
    with pytest.raises(ValueError, match="match exactly"):
        report("unbound", "A claim with an unbound footnote[^missing]", {})
    value = envelope(root)
    malformed = replace(
        root,
        content=json.dumps(
            {
                "schema": [value["schema"]],
                "kind": [value["kind"]],
                "parents": [],
                "payload": {},
            },
            separators=(",", ":"),
        ),
    )
    with pytest.raises(ValueError, match="artifact-v1 envelope"):
        envelope(malformed)
    store.close()


def test_sqlite_reopen_and_rebuild_retains_artifact_lineage(tmp_path) -> None:
    path = tmp_path / "artifacts.sqlite"
    store = SQLiteStore(str(path))
    _runtime, root, _doc, _ev, rep = _chain(store)
    before = lineage(rep.id, store.get_asset)
    before_export = render_bundle(rep.id, store.get_asset, include_sources=True)
    before_hashes = {
        name: hashlib.sha256(data).hexdigest() for name, data in before_export.items()
    }
    store.close()
    reconstruction = verify_reconstruction(path, tmp_path / "reconstruction")
    assert reconstruction["status"] == "passed", reconstruction
    rebuilt = SQLiteStore(str(tmp_path / "reconstruction" / "rebuilt.sqlite"))
    assert lineage(rep.id, rebuilt.get_asset) == before
    assert attachment_bytes(rebuilt.get_asset(root.id)) == b"raw bytes"
    after_export = render_bundle(rep.id, rebuilt.get_asset, include_sources=True)
    assert {
        name: hashlib.sha256(data).hexdigest() for name, data in after_export.items()
    } == before_hashes
    rebuilt.close()


def test_citation_anchors_and_json_are_unambiguous():
    with pytest.raises(ValueError, match="ignoring case"):
        report("r", "One[^A], two[^a]", {"A": "id1", "a": "id2"})
    source = attachment("source", b"text", media_type="text/plain")
    duplicate = replace(
        source, content=source.content.replace('"kind":', '"kind":"report","kind":', 1)
    )
    with pytest.raises(ValueError, match="duplicate JSON"):
        envelope(duplicate)
