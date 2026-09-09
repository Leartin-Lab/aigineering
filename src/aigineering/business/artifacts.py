"""Portable attachments and evidence bindings over signed text Assets.

The envelope is a business convention, not a new kernel fact type. Validation
proves byte integrity and exact quotation, never semantic entailment or access
rights. All functions here are pure; callers publish returned proposals through
their ordinary, explicitly authorized Candidate path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping

from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.ids import canonical_json
from aigineering.protocol.types import Asset

MEDIA_TYPE = "application/vnd.aigineering.artifact+json"
SCHEMA = "artifact-v1"
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_NODES = 1000
KINDS = {"attachment", "document", "evidence", "report"}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member in artifact")
        value[key] = item
    return value


def envelope(asset: Asset) -> dict:
    """Read a visible envelope; sealed content cannot enter derived exports."""
    if asset.disclosure_view != "original" or asset.tombstoned:
        raise ValueError("asset is not available for disclosure")
    if asset.content_type not in (MEDIA_TYPE, "text", "text/plain", "application/json"):
        raise ValueError("asset is not an artifact-v1 envelope")
    value = json.loads(asset.content, object_pairs_hook=_unique_object)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "kind", "parents", "payload"}
        or value["schema"] != SCHEMA
        or not isinstance(value["kind"], str)
        or value["kind"] not in KINDS
        or not isinstance(value["parents"], list)
        or not all(isinstance(p, str) and p for p in value["parents"])
        or len(value["parents"]) != len(set(value["parents"]))
        or not isinstance(value["payload"], dict)
    ):
        raise ValueError("invalid artifact-v1 envelope")
    return value


def proposal(name: str, kind: str, payload: Mapping, parents=()) -> Asset:
    """Build an unsigned proposal; no fact exists until Candidate acceptance."""
    if kind not in KINDS:
        raise ValueError("unsupported artifact kind")
    result = build_control_plane_asset(
        name=name,
        content=canonical_json(
            {
                "schema": SCHEMA,
                "kind": kind,
                "parents": list(parents),
                "payload": payload,
            }
        ),
        content_type=MEDIA_TYPE,
        origin="artifact-adapter",
        trust_tier="untrusted",
        promptable=kind != "attachment",
    )
    envelope(result)
    return result


def attachment(
    name: str,
    data: bytes,
    *,
    media_type: str,
    source_uri: str = "",
    license: str = "unknown",
    retrieved_at: str = "",
) -> Asset:
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("attachment exceeds 8 MiB limit")
    return proposal(
        name,
        "attachment",
        {
            "encoding": "base64",
            "data": base64.b64encode(data).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "media_type": media_type,
            "source_uri": source_uri,
            "license": license,
            "retrieved_at": retrieved_at,
        },
    )


def attachment_bytes(asset: Asset) -> bytes:
    value = envelope(asset)
    if value["kind"] != "attachment" or value["parents"]:
        raise ValueError("expected root attachment")
    p = value["payload"]
    if p.get("encoding") != "base64" or not isinstance(p.get("data"), str):
        raise ValueError("unsupported attachment encoding")
    if len(p["data"]) > 4 * ((MAX_ATTACHMENT_BYTES + 2) // 3):
        raise ValueError("attachment exceeds 8 MiB limit")
    try:
        data = base64.b64decode(p["data"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid attachment base64") from exc
    if (
        type(p.get("size")) is not int
        or len(data) != p["size"]
        or len(data) > MAX_ATTACHMENT_BYTES
        or hashlib.sha256(data).hexdigest() != p.get("sha256")
    ):
        raise ValueError("attachment byte integrity mismatch")
    if not all(
        isinstance(p.get(k), str)
        for k in ("media_type", "source_uri", "license", "retrieved_at")
    ):
        raise ValueError("invalid attachment metadata")
    return data


def document(name: str, source: Asset, extraction: Mapping) -> Asset:
    attachment_bytes(source)
    _validate_extraction(extraction)
    return proposal(name, "document", dict(extraction), (source.id,))


def _validate_extraction(p: Mapping) -> None:
    if p.get("schema") != "document-extraction-v1":
        raise ValueError("unsupported extraction schema")
    if not isinstance(p.get("tool"), str) or not p["tool"]:
        raise ValueError("extraction requires tool identity")
    if not isinstance(p.get("version"), str) or not p["version"]:
        raise ValueError("extraction requires tool version")
    if type(p.get("ocr")) is not bool:
        raise ValueError("extraction requires explicit OCR status")
    pages = p.get("pages")
    if not isinstance(pages, (list, tuple)) or not pages:
        raise ValueError("extraction requires pages")
    for number, page in enumerate(pages, 1):
        if (
            not isinstance(page, Mapping)
            or type(page.get("page")) is not int
            or page["page"] != number
            or not isinstance(page.get("text"), str)
            or unicodedata.normalize("NFC", page["text"]) != page["text"]
        ):
            raise ValueError("pages require consecutive 1-based indexes and NFC text")


def evidence(name: str, source: Asset, *, page: int, start: int, end: int) -> Asset:
    value = envelope(source)
    if value["kind"] != "document":
        raise ValueError("evidence requires a document asset")
    _validate_extraction(value["payload"])
    pages = value["payload"]["pages"]
    if any(type(i) is not int for i in (page, start, end)) or not 1 <= page <= len(
        pages
    ):
        raise ValueError("invalid page or character bounds")
    text = pages[page - 1]["text"]
    if not 0 <= start < end <= len(text):
        raise ValueError("invalid page or character bounds")
    return proposal(
        name,
        "evidence",
        {
            "page": page,
            "start": start,
            "end": end,
            "quote": text[start:end],
            "selector": "page-nfc-chars-v1",
        },
        (source.id,),
    )


def report(name: str, markdown: str, citations: Mapping[str, str]) -> Asset:
    """Bind every supported footnote marker to an exact evidence Asset ID.

    The renderer owns footnote definitions. Footnote-like tokens inside code are
    deliberately treated as citations too; this small dialect fails closed.
    """
    if not isinstance(markdown, str) or not isinstance(citations, Mapping):
        raise ValueError("report requires Markdown and a citation mapping")
    if any(
        not isinstance(k, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", k)
        or not isinstance(v, str)
        or not v
        for k, v in citations.items()
    ):
        raise ValueError("invalid citation anchor or asset ID")
    if len({key.lower() for key in citations}) != len(citations):
        raise ValueError("citation anchors must be unique ignoring case")
    if re.search(r"\[\^[^\]]+\]:", markdown):
        raise ValueError("footnote definitions are generated from citation bindings")
    anchors = re.findall(r"\[\^([^\]]+)\]", markdown)
    if set(anchors) != set(citations):
        raise ValueError("Markdown citations and bindings must match exactly")
    return proposal(
        name,
        "report",
        {"markdown": markdown, "citations": dict(citations)},
        tuple(sorted(set(citations.values()))),
    )


def lineage(
    root_id: str, get_asset: Callable[[str], Asset | None], *, records=()
) -> dict:
    """Validate and recursively resolve a bounded, exact-ID artifact DAG.

    Supply a visibility-filtered lookup for non-administrator consumers. Missing
    or withheld nodes fail closed. Parser fidelity and entailment stay unproven.
    """
    nodes: dict[str, dict] = {}
    active: set[str] = set()

    def visit(asset_id: str, depth: int = 0) -> None:
        if asset_id in active:
            raise ValueError("artifact lineage contains a cycle")
        if asset_id in nodes:
            return
        if len(nodes) + len(active) >= MAX_NODES or depth > 100:
            raise ValueError("artifact lineage exceeds traversal limit")
        asset = get_asset(asset_id)
        if asset is None or asset.id != asset_id:
            raise ValueError("lineage asset missing or not visible")
        value = envelope(asset)
        active.add(asset_id)
        for parent in value["parents"]:
            visit(parent, depth + 1)
        kind, p, parents = value["kind"], value["payload"], value["parents"]
        if kind == "attachment":
            attachment_bytes(asset)
        elif kind == "document":
            _validate_extraction(p)
            if len(parents) != 1 or nodes[parents[0]]["kind"] != "attachment":
                raise ValueError("document requires one attachment parent")
        elif kind == "evidence":
            if len(parents) != 1 or nodes[parents[0]]["kind"] != "document":
                raise ValueError("evidence requires one document parent")
            parent = get_asset(parents[0])
            expected = envelope(
                evidence(
                    "verify",
                    parent,
                    page=p.get("page"),
                    start=p.get("start"),
                    end=p.get("end"),
                )
            )["payload"]
            if p != expected:
                raise ValueError("evidence does not equal the exact source range")
        elif kind == "report":
            expected = envelope(report("verify", p.get("markdown"), p.get("citations")))
            if expected["parents"] != parents or any(
                nodes[x]["kind"] != "evidence" for x in parents
            ):
                raise ValueError("report requires exact evidence parents")
        active.remove(asset_id)
        nodes[asset_id] = {
            "id": asset_id,
            "kind": kind,
            "parents": parents,
            "signed_by": asset.signed_by,
            "created_by": asset.created_by,
            "verification": "exact_quote"
            if kind == "evidence"
            else "byte_integrity"
            if kind == "attachment"
            else "asserted",
        }
        metadata_keys = {
            "attachment": (
                "sha256",
                "size",
                "media_type",
                "source_uri",
                "license",
                "retrieved_at",
            ),
            "document": ("tool", "version", "ocr"),
            "evidence": ("selector", "page", "start", "end"),
            "report": (),
        }[kind]
        nodes[asset_id]["metadata"] = {key: p[key] for key in metadata_keys}

    visit(root_id)
    result = {
        "schema": "artifact-lineage-v1",
        "root": root_id,
        "nodes": [nodes[k] for k in sorted(nodes)],
        "complete": True,
        "semantic_support": "not_evaluated",
    }
    if records:
        # Record metadata is optional and must itself be authorized by callers.
        # Never export receipt payloads: they may contain unrelated/sealed data.
        by_id = {record.id: record for record in records}
        publications = [
            record
            for record in by_id.values()
            if record.record_type == "asset.committed"
            and isinstance(record.payload.get("asset"), Mapping)
            and record.payload["asset"].get("id") in nodes
        ]
        pending = [record.id for record in publications]
        selected = {}
        missing = set()
        while pending:
            record_id = pending.pop()
            if record_id in selected or record_id in missing:
                continue
            if len(selected) + len(missing) >= 10000:
                raise ValueError("publication ancestry exceeds traversal limit")
            record = by_id.get(record_id)
            if record is None:
                missing.add(record_id)
                continue
            selected[record_id] = {
                "id": record.id,
                "type": record.record_type,
                "parents": list(record.causal_parents),
                **{
                    key: record.payload[key]
                    for key in ("candidate_id", "actor_id", "key_id", "contract_id")
                    if isinstance(record.payload.get(key), str)
                },
            }
            pending.extend(record.causal_parents)
        result["publications"] = [
            {"asset_id": record.payload["asset"]["id"], "record_id": record.id}
            for record in sorted(publications, key=lambda item: item.id)
        ]
        result["runtime_ancestry"] = [selected[key] for key in sorted(selected)]
        result["unresolved_runtime_parents"] = sorted(missing)
    return result
