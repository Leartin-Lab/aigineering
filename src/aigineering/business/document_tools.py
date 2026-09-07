"""Small, side effect free adapters for extracting document text.

The functions in this module return observations only.  They deliberately do
not publish facts or interact with the runtime store.
"""

from __future__ import annotations

import importlib
import platform
import unicodedata
from typing import Any

# These are module settings so an embedding application (or a test) can tune
# resource limits without making third party dependencies mandatory.
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 1_000


class DocumentExtractionError(ValueError):
    """Raised when an input cannot be safely extracted."""


def _result(
    *, pages: list[dict[str, Any]], tool: str, version: str, ocr: bool
) -> dict[str, Any]:
    return {
        "schema": "document-extraction-v1",
        "pages": pages,
        "tool": tool,
        "version": version,
        "ocr": ocr,
    }


def _text(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def extract_text(data: bytes) -> dict[str, Any]:
    """Extract UTF-8 text bytes into the common one-based page schema."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentExtractionError("text is not valid UTF-8") from exc
    return _result(
        pages=[{"page": 1, "text": _text(decoded)}],
        tool="stdlib",
        version=platform.python_version(),
        ocr=False,
    )


def _load_pdf_library() -> tuple[Any, str, str]:
    """Load the optional PyMuPDF package under its unambiguous import name."""

    try:
        module = importlib.import_module("pymupdf")
    except ImportError as exc:
        raise RuntimeError(
            "PDF extraction requires optional PyMuPDF; install the 'pymupdf' package"
        ) from exc
    version = getattr(module, "__version__", None)
    if version is None:
        versions = getattr(module, "version", None)
        if isinstance(versions, (tuple, list)) and versions:
            version = versions[0]
        elif versions:
            version = str(versions)
    return module, "pymupdf", str(version or "unknown")


def extract_pdf(data: bytes, *, ocr: bool = False) -> dict[str, Any]:
    """Extract PDF pages, optionally using PyMuPDF's OCR text page support.

    OCR is explicit: requesting it requires ``get_textpage_ocr`` and any OCR
    error is raised to the caller rather than falling back to embedded text.
    """

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if type(ocr) is not bool:
        raise TypeError("ocr must be bool")
    if len(data) > MAX_PDF_BYTES:
        raise DocumentExtractionError(
            f"PDF exceeds maximum size of {MAX_PDF_BYTES} bytes"
        )

    library, tool, version = _load_pdf_library()
    try:
        document = library.open(stream=data, filetype="pdf")
    except Exception as exc:  # library-specific malformed-PDF exceptions vary
        raise DocumentExtractionError(f"invalid PDF: {exc}") from exc

    try:
        try:
            page_count = len(document)
        except Exception as exc:
            raise DocumentExtractionError(f"invalid PDF page count: {exc}") from exc
        if page_count < 1:
            raise DocumentExtractionError("PDF must contain at least one page")
        if page_count > MAX_PDF_PAGES:
            raise DocumentExtractionError(
                f"PDF exceeds maximum page count of {MAX_PDF_PAGES}"
            )

        pages: list[dict[str, Any]] = []
        for index in range(page_count):
            page = document[index]
            if ocr:
                ocr_factory = getattr(page, "get_textpage_ocr", None)
                if not callable(ocr_factory):
                    raise RuntimeError(
                        "OCR requested but this PyMuPDF build lacks get_textpage_ocr"
                    )
                textpage = ocr_factory()
                text = page.get_text("text", textpage=textpage)
            else:
                text = page.get_text("text")
            pages.append({"page": index + 1, "text": _text(text)})
        return _result(pages=pages, tool=tool, version=version, ocr=bool(ocr))
    finally:
        close = getattr(document, "close", None)
        if callable(close):
            close()


__all__ = [
    "DocumentExtractionError",
    "MAX_PDF_BYTES",
    "MAX_PDF_PAGES",
    "extract_pdf",
    "extract_text",
]
