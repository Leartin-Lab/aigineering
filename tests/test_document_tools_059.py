from __future__ import annotations

import importlib.util
import sys
import types

import pytest

from aigineering.business.document_tools import (
    DocumentExtractionError,
    extract_pdf,
    extract_text,
)


def test_extract_text_has_stable_schema_and_nfc() -> None:
    result = extract_text("e\u0301".encode())
    assert result == {
        "schema": "document-extraction-v1",
        "pages": [{"page": 1, "text": "é"}],
        "tool": "stdlib",
        "version": result["version"],
        "ocr": False,
    }


def test_extract_text_rejects_invalid_utf8() -> None:
    with pytest.raises(DocumentExtractionError, match="UTF-8"):
        extract_text(b"\xff")


def test_pdf_adapter_with_mock_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        def __init__(self, text: str) -> None:
            self.text = text
            self.ocr_called = False

        def get_textpage_ocr(self) -> object:
            self.ocr_called = True
            return object()

        def get_text(self, kind: str, *, textpage: object | None = None) -> str:
            assert kind == "text"
            return self.text

    pages = [Page("a\u0301"), Page("two")]

    class Document:
        def __len__(self) -> int:
            return len(pages)

        def __getitem__(self, index: int) -> Page:
            return pages[index]

        def close(self) -> None:
            pass

    module = types.SimpleNamespace(__version__="9.9", open=lambda **_: Document())
    monkeypatch.setitem(sys.modules, "pymupdf", module)
    result = extract_pdf(b"%PDF-mock", ocr=True)
    assert result["pages"] == [{"page": 1, "text": "á"}, {"page": 2, "text": "two"}]
    assert result["tool"] == "pymupdf"
    assert result["version"] == "9.9"
    assert result["ocr"] is True
    assert all(page.ocr_called for page in pages)


def test_pdf_malformed_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.SimpleNamespace(
        open=lambda **_: (_ for _ in ()).throw(RuntimeError("bad"))
    )
    monkeypatch.setitem(sys.modules, "pymupdf", module)
    with pytest.raises(DocumentExtractionError, match="invalid PDF"):
        extract_pdf(b"bad")


def test_pdf_rejects_empty_document_and_non_boolean_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyDocument:
        def __len__(self) -> int:
            return 0

        def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules, "pymupdf", types.SimpleNamespace(open=lambda **_: EmptyDocument())
    )
    with pytest.raises(TypeError, match="ocr must be bool"):
        extract_pdf(b"pdf", ocr=1)  # type: ignore[arg-type]
    with pytest.raises(DocumentExtractionError, match="at least one page"):
        extract_pdf(b"pdf")


def test_pdf_page_limit_and_one_based_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    class Document:
        def __len__(self) -> int:
            return 3

        def __getitem__(self, index: int) -> object:
            return types.SimpleNamespace(get_text=lambda *_args, **_kwargs: str(index))

        def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules, "pymupdf", types.SimpleNamespace(open=lambda **_: Document())
    )
    import aigineering.business.document_tools as tools

    monkeypatch.setattr(tools, "MAX_PDF_PAGES", 2)
    with pytest.raises(DocumentExtractionError, match="page count"):
        extract_pdf(b"pdf")
    monkeypatch.setattr(tools, "MAX_PDF_PAGES", 4)
    assert [p["page"] for p in extract_pdf(b"pdf")["pages"]] == [1, 2, 3]


def test_real_small_pdf_when_pymupdf_is_available() -> None:
    if importlib.util.find_spec("pymupdf") is None:
        pytest.skip("PyMuPDF is not installed")
    import pymupdf

    with pymupdf.open() as document:
        for content in ("Evidence on page one.", "An independent second page."):
            page = document.new_page()
            page.insert_text((72, 72), content)
        data = document.tobytes()
    result = extract_pdf(data)
    assert result["ocr"] is False
    assert result["pages"] == [
        {"page": 1, "text": "Evidence on page one.\n"},
        {"page": 2, "text": "An independent second page.\n"},
    ]
    with pytest.raises(DocumentExtractionError):
        extract_pdf(b"%PDF-1.4\n% malformed enough for dependency check")
