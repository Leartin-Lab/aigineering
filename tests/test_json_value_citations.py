"""Exact JSON scalar citations use ordinary signed Assets and artifact lineage."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from conftest import candidate_runtime
from aigineering.business.artifacts import (
    envelope,
    json_evidence,
    lineage,
    report,
)
from aigineering.business.export import render_bundle
from aigineering.core.control_plane import build_control_plane_asset
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


def _json_source(content: str, *, name: str = "tool-observation"):
    return build_control_plane_asset(
        name=name,
        content=content,
        content_type="application/json",
        origin="test-tool",
        trust_tier="untrusted",
        promptable=True,
    )


def test_json_value_citation_preserves_numeric_lexeme_and_pointer_escaping():
    source = _json_source(r'{"a/b":{"~key":[0,1.2300e+400]},"secret":"do not export"}')
    cited = json_evidence("precise-value", source, "/a~1b/~0key/1")

    value = envelope(cited)
    assert value["parents"] == [source.id]
    assert value["payload"] == {
        "selector": "asset-json-pointer-v1",
        "pointer": "/a~1b/~0key/1",
        "quote": "1.2300e+400",
        "value_type": "number",
        "source_sha256": hashlib.sha256(source.content.encode("utf-8")).hexdigest(),
    }


@pytest.mark.parametrize(
    ("source_text", "pointer", "quote", "value_type"),
    [
        (
            '{"message":"<tag> & \\"quoted\\""}',
            "/message",
            '<tag> & "quoted"',
            "string",
        ),
        ('{"flag":false}', "/flag", "false", "boolean"),
        ('{"empty":null}', "/empty", "null", "null"),
        ("17", "", "17", "number"),
    ],
)
def test_json_value_citation_supports_scalar_values(
    source_text: str, pointer: str, quote: str, value_type: str
):
    cited = json_evidence("value", _json_source(source_text), pointer)
    payload = envelope(cited)["payload"]
    assert payload["quote"] == quote
    assert payload["value_type"] == value_type


@pytest.mark.parametrize(
    ("source_text", "pointer"),
    [
        ('{"x":1,"x":2}', "/x"),
        ('{"x":NaN}', "/x"),
        ('{"x":Infinity}', "/x"),
        ('{"x":1}', "x"),
        ('{"x":1}', "/missing"),
        ('{"x":[]}', "/x/00"),
        ('{"x":[]}', "/x/-"),
        ('{"x":{}}', "/x"),
        ('{"x":[]}', "/x"),
    ],
)
def test_json_value_citation_rejects_malformed_or_composite_targets(
    source_text: str, pointer: str
):
    with pytest.raises(ValueError):
        json_evidence("bad", _json_source(source_text), pointer)


@pytest.mark.parametrize(
    "source",
    [
        replace(_json_source('{"x":1}'), promptable=False),
        replace(_json_source('{"x":1}'), disclosure_view="sealed"),
        replace(_json_source('{"x":1}'), tombstoned=True),
        replace(_json_source('{"x":1}'), content_type="application/octet-stream"),
    ],
)
def test_json_value_citation_rejects_unavailable_sources(source):
    with pytest.raises(ValueError):
        json_evidence("hidden", source, "/x")


def test_json_value_lineage_recomputes_quote_digest_and_exact_parent():
    source = _json_source('{"count":9007199254740993}')
    cited = json_evidence("count", source, "/count")
    assets = {source.id: source, cited.id: cited}
    result = lineage(cited.id, assets.get)
    assert {node["id"] for node in result["nodes"]} == {source.id, cited.id}

    payload = envelope(cited)["payload"]
    for changed in (
        replace(
            cited,
            content=cited.content.replace("9007199254740993", "9007199254740994"),
        ),
        replace(source, content='{"count":9007199254740994}'),
        replace(source, disclosure_view="sealed"),
    ):
        altered = dict(assets)
        altered[changed.id] = changed
        with pytest.raises(ValueError):
            lineage(cited.id, altered.get)

    wrong_parent = replace(cited, content=cited.content.replace(source.id, "other-id"))
    with pytest.raises(ValueError):
        lineage(wrong_parent.id, {wrong_parent.id: wrong_parent, source.id: source}.get)
    assert payload["quote"] == "9007199254740993"


def _signed_json_report(store: SQLiteStore):
    runtime = candidate_runtime(store)
    raw = '{"price":1.234567890123456789e+120,"private":"SECRET-SIBLING"}'
    source = _publish(runtime, _json_source(raw), "json-source")
    ev = _publish(runtime, json_evidence("price", source, "/price"), "json-evidence")
    rep = _publish(
        runtime,
        report("json-report", "Measured value[^price].", {"price": ev.id}),
        "json-report",
    )
    return source, ev, rep, raw


def test_signed_json_report_export_is_value_only_unless_sources_requested(tmp_path):
    path = tmp_path / "json-citations.sqlite"
    store = SQLiteStore(str(path))
    source, ev, rep, raw = _signed_json_report(store)

    default = render_bundle(rep.id, store.get_asset)
    html = default["evidence/price.html"].decode("utf-8")
    assert "1.234567890123456789e+120" in html
    assert "SECRET-SIBLING" not in html
    assert "&lt;" not in html  # selected value has no HTML markup to escape
    assert not any(data == raw.encode("utf-8") for data in default.values())
    assert json.loads(default["manifest.json"])["includes_sources"] is False

    full = render_bundle(rep.id, store.get_asset, include_sources=True)
    assert raw.encode("utf-8") in full.values()
    manifest = json.loads(full["manifest.json"])
    assert manifest["includes_sources"] is True
    assert manifest["files"] == {
        name: hashlib.sha256(data).hexdigest()
        for name, data in sorted(full.items())
        if name != "manifest.json"
    }
    assert envelope(ev)["parents"] == [source.id]
    store.close()


def test_json_value_report_survives_reopen_and_reconstruction(tmp_path):
    path = tmp_path / "json-rebuild.sqlite"
    store = SQLiteStore(str(path))
    _source, _ev, rep, _raw = _signed_json_report(store)
    before = render_bundle(rep.id, store.get_asset, include_sources=True)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in before.items()}
    store.close()

    reconstruction = verify_reconstruction(path, tmp_path / "reconstruction")
    assert reconstruction["status"] == "passed", reconstruction
    rebuilt = SQLiteStore(str(tmp_path / "reconstruction" / "rebuilt.sqlite"))
    after = render_bundle(rep.id, rebuilt.get_asset, include_sources=True)
    assert {
        name: hashlib.sha256(data).hexdigest() for name, data in after.items()
    } == hashes
    rebuilt.close()
