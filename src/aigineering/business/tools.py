"""Explicit, scope-bound ToolRegistry composition for business artifacts."""

from __future__ import annotations

from aigineering.business.artifacts import attachment_bytes, envelope, lineage
from aigineering.business.document_tools import extract_pdf, extract_text
from aigineering.business.fulltext import fulltext_locations
from aigineering.core.ids import canonical_json
from aigineering.core.tools import ToolRegistry
from aigineering.protocol.types import Asset, ToolSpec


def build_registry(*, assets: tuple[Asset, ...] = ()) -> ToolRegistry:
    """Bind a registry to an explicitly authorized immutable asset snapshot.

    No Store handle, name lookup, disk path or ambient database is accessible to
    these handlers. Construct separate registries for different disclosure
    scopes. Tool observations still require ordinary Worker output publication
    and independent acceptance when the Contract requires it.
    """
    visible = {asset.id: asset for asset in assets}
    if len(visible) != len(assets):
        raise ValueError("duplicate asset IDs in tool scope")
    registry = ToolRegistry()

    def check(args):
        return canonical_json(lineage(args["asset_id"], visible.get))

    def extract(args):
        asset = visible.get(args["asset_id"])
        if asset is None:
            raise ValueError("asset outside configured tool scope")
        data = attachment_bytes(asset)
        media = envelope(asset)["payload"]["media_type"]
        if media == "application/pdf":
            result = extract_pdf(data, ocr=args.get("ocr", False))
        elif media == "text/plain" and not args.get("ocr", False):
            result = extract_text(data)
        else:
            raise ValueError("unsupported document media or OCR request")
        return canonical_json(result)

    for name, description, handler, extra in (
        (
            "artifact_verify",
            "Verify byte integrity, exact quotations and ancestry; does not evaluate semantic support.",
            check,
            {},
        ),
        (
            "document_extract",
            "Extract page text from an explicitly scoped attachment; returns an untrusted observation.",
            extract,
            {"ocr": {"type": "boolean"}},
        ),
    ):
        registry.register(
            ToolSpec(
                name=name,
                description=description,
                version="1.0.0",
                input_schema={
                    "type": "object",
                    "required": ["asset_id"],
                    "additionalProperties": False,
                    "properties": {
                        "asset_id": {"type": "string", "minLength": 1},
                        **extra,
                    },
                },
                output_schema={"type": "object"},
                max_output_bytes=1024 * 1024,
            ),
            handler,
        )
    registry.register(
        ToolSpec(
            name="fulltext_locations",
            description="Resolve full-text candidates from supplied provider metadata; grants no access.",
            version="1.0.0",
            input_schema={
                "type": "object",
                "required": ["provider", "record"],
                "additionalProperties": False,
                "properties": {
                    "provider": {"type": "string", "enum": ["openalex", "unpaywall"]},
                    "record": {"type": "object"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            max_output_bytes=262144,
        ),
        lambda args: canonical_json(
            fulltext_locations(args["provider"], args["record"])
        ),
    )
    return registry
