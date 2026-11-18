"""Explicit local ingress and read-only export for standard business artifacts."""

from __future__ import annotations

import json
from http.client import HTTPException
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import click

from aigineering.business import artifacts
from aigineering.business.document_tools import extract_pdf, extract_text
from aigineering.business.export import export_bundle
from aigineering.business.fulltext import fulltext_locations
from aigineering.cli._candidate import commit_local_effect, require_accepted
from aigineering.cli._common import _output_json, _persistent_store
from aigineering.protocol.effect_builders import asset_proposal_effect


def _errors(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValueError, LookupError, OSError, RuntimeError, HTTPException) as exc:
            raise click.ClickException(str(exc)) from exc

    return wrapped


def _get(store, asset_id):
    value = store.get_asset(asset_id)
    if value is None:
        raise ValueError("exact asset ID not found")
    artifacts.envelope(value)
    return value


def _publish(store, proposal):
    # Validate the business closure before administrative ingress. The kernel
    # still owns signature, authority and durable publication, not this adapter.
    artifacts.lineage(
        proposal.id,
        lambda key: proposal if key == proposal.id else store.get_asset(key),
    )
    decision = require_accepted(
        commit_local_effect(
            store,
            asset_proposal_effect(proposal),
            idempotency_key=f"artifact:{proposal.id}",
        )
    )
    asset = decision.assets[0]
    _output_json(
        {"id": asset.id, "name": asset.name, "kind": artifacts.envelope(asset)["kind"]}
    )


def _lineage_records(store, graph):
    """Select metadata for this exact closure without reading unrelated history."""
    selected = {
        record.id: record
        for node in graph["nodes"]
        for record in store.read_asset_commit_facts(node["id"])
    }
    pending = list(selected.values())
    seen = set(selected)
    if len(seen) > 10000:
        raise ValueError("publication ancestry exceeds traversal limit")
    while pending:
        record = pending.pop()
        for parent_id in record.causal_parents:
            if parent_id in seen:
                continue
            seen.add(parent_id)
            if len(seen) > 10000:
                raise ValueError("publication ancestry exceeds traversal limit")
            parent = store.get_runtime_record(parent_id)
            if parent is not None:
                selected[parent_id] = parent
                pending.append(parent)
    return tuple(selected.values())


@click.group("artifact")
def artifact_group():
    """Import, cite, inspect and export exact versioned business artifacts."""


@artifact_group.command("import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--name", required=True)
@click.option("--media-type", required=True)
@click.option("--source-uri", default="")
@click.option("--license", "license_value", default="unknown")
@_errors
def import_file(file, name, media_type, source_uri, license_value):
    """Commit an exact local file in a bounded, self-contained attachment."""
    with file.open("rb") as stream:
        data = stream.read(artifacts.MAX_ATTACHMENT_BYTES + 1)
    proposal = artifacts.attachment(
        name,
        data,
        media_type=media_type,
        source_uri=source_uri,
        license=license_value,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
    )
    store = _persistent_store()
    try:
        _publish(store, proposal)
    finally:
        store.close()


@artifact_group.command("parse")
@click.argument("asset_id")
@click.option("--name", required=True)
@click.option(
    "--ocr", is_flag=True, help="Explicit PDF OCR; requires local Tesseract support."
)
@_errors
def parse_file(asset_id, name, ocr):
    """Extract a committed attachment into versioned page text."""
    store = _persistent_store()
    try:
        source = _get(store, asset_id)
        data = artifacts.attachment_bytes(source)
        media = artifacts.envelope(source)["payload"]["media_type"]
        if media == "application/pdf":
            extraction = extract_pdf(data, ocr=ocr)
        elif media == "text/plain" and not ocr:
            extraction = extract_text(data)
        else:
            raise ValueError(
                "supported extraction media: application/pdf or text/plain (without OCR)"
            )
        _publish(store, artifacts.document(name, source, extraction))
    finally:
        store.close()


@artifact_group.command("cite")
@click.argument("asset_id")
@click.option("--name", required=True)
@click.option("--page", type=int, help="1-based physical page index.")
@click.option("--start", type=int, help="0-based NFC character offset.")
@click.option("--end", type=int, help="Exclusive NFC character offset.")
@click.option(
    "--pointer",
    help="JSON Pointer to a scalar in a disclosed JSON Asset; empty selects the root.",
)
@_errors
def cite(asset_id, name, page, start, end, pointer):
    """Cite a JSON scalar (--pointer) or document text (--page/--start/--end).

    Select exactly one mode. JSON Pointer escapes / as ~1 and ~ as ~0.
    """
    bounds = (page, start, end)
    if (pointer is not None and any(x is not None for x in bounds)) or (
        pointer is None and any(x is None for x in bounds)
    ):
        raise ValueError("use --pointer OR all of --page, --start and --end")
    store = _persistent_store()
    try:
        if pointer is not None:
            source = store.get_asset(asset_id)
            if source is None:
                raise ValueError("exact asset ID not found")
            proposal = artifacts.json_evidence(name, source, pointer)
        else:
            proposal = artifacts.evidence(
                name, _get(store, asset_id), page=page, start=start, end=end
            )
        _publish(store, proposal)
    finally:
        store.close()


@artifact_group.command("report")
@click.argument(
    "markdown", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--bindings",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="JSON object mapping footnote anchors to exact evidence asset IDs.",
)
@click.option("--name", required=True)
@_errors
def report(markdown, bindings, name):
    """Commit Markdown and its complete citation binding together."""
    proposal = artifacts.report(
        name,
        markdown.read_text(encoding="utf-8"),
        json.loads(bindings.read_text(encoding="utf-8")),
    )
    store = _persistent_store()
    try:
        _publish(store, proposal)
    finally:
        store.close()


@artifact_group.command("show")
@click.argument("asset_id")
@_errors
def show(asset_id):
    """Show exact envelope data, excluding attachment bytes."""
    store = _persistent_store()
    try:
        value = artifacts.envelope(_get(store, asset_id))
        if value["kind"] == "attachment":
            value["payload"].pop("data", None)
        _output_json({"id": asset_id, **value})
    finally:
        store.close()


@artifact_group.command("lineage")
@click.argument("asset_id")
@_errors
def lineage(asset_id):
    """Validate every ancestor and print the complete exact-ID graph."""
    store = _persistent_store()
    try:
        with store.read_transaction():
            graph = artifacts.lineage(asset_id, store.get_asset)
            result = artifacts.lineage(
                asset_id,
                store.get_asset,
                records=_lineage_records(store, graph),
            )
        _output_json(result)
    finally:
        store.close()


@artifact_group.command("export")
@click.argument("report_id")
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option(
    "--include-sources",
    is_flag=True,
    help="Also redistribute original attachments or JSON sources.",
)
@_errors
def export(report_id, output, include_sources):
    """Export a new offline Markdown/evidence/lineage directory."""
    store = _persistent_store()
    try:
        with store.read_transaction():
            graph = artifacts.lineage(report_id, store.get_asset)
            export_bundle(
                report_id,
                store.get_asset,
                output,
                include_sources=include_sources,
                records=_lineage_records(store, graph),
            )
        _output_json({"report_id": report_id, "output": str(output.absolute())})
    finally:
        store.close()


@artifact_group.command("locations")
@click.argument(
    "metadata", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--provider", type=click.Choice(["openalex", "unpaywall"]), required=True)
@_errors
def locations(metadata, provider):
    """Resolve candidates from downloaded metadata; does not fetch or grant access."""
    _output_json(
        fulltext_locations(provider, json.loads(metadata.read_text(encoding="utf-8")))
    )


@artifact_group.command("fetch")
@click.argument("url")
@click.option(
    "--allow-host",
    multiple=True,
    required=True,
    help="Exact allowed host, repeated for redirect hosts.",
)
@click.option("--name", required=True)
@_errors
def fetch(url, allow_host, name):
    """Acquire public HTTPS content and commit bytes plus the acquisition receipt."""
    from aigineering.business.acquisition import fetch_public

    data, receipt = fetch_public(url, allowed_hosts=allow_host)
    value = artifacts.attachment(
        name,
        data,
        media_type=receipt["media_type"],
        source_uri=receipt["source_uri"],
        retrieved_at=receipt["retrieved_at"],
    )
    payload = artifacts.envelope(value)["payload"]
    payload["acquisition"] = receipt
    value = artifacts.proposal(name, "attachment", payload)
    store = _persistent_store()
    try:
        _publish(store, value)
    finally:
        store.close()
