"""anydoc extraction entry point: fast path + OCR chain + Thai gate."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.doc_extraction as doc_extraction
from app.doc_extraction import anydoc_supports, extract_document_text


class _FakeAnydoc:
    def __init__(self, markdown: str = "พระราชบัญญัติตัวอย่าง เนื้อหาภาษาไทยที่ยาวพอสำหรับ gate ของระบบ "):
        self.markdown = markdown
        self.calls: list[tuple[bytes, str | None, object]] = []

    def format_from_path(self, path) -> str | None:
        return "pdf" if str(path).endswith(".pdf") else "docx"

    def to_markdown_with_ocr(self, data, fmt, ocr=None):
        assert callable(ocr), "pyo3 binding requires a callable, not an object"
        self.calls.append((data, fmt, ocr))
        return self.markdown


def _document(tmp_path: Path, name: str) -> SimpleNamespace:
    target = tmp_path / name
    target.write_bytes(b"fake-bytes")
    return SimpleNamespace(storage_path=str(target))


def test_anydoc_supports_covers_native_formats():
    assert anydoc_supports("a.pdf") and anydoc_supports("b.DOCX") and anydoc_supports("c.csv")
    assert not anydoc_supports("page.html")


def test_anydoc_supports_excludes_html():
    assert not anydoc_supports("page.html")
    assert not anydoc_supports("page.htm")


def test_unavailable_wheel_raises_specific_error(monkeypatch):
    monkeypatch.setattr(doc_extraction, "ANYDOC_AVAILABLE", False)
    with pytest.raises(RuntimeError, match="ANYDOC_UNAVAILABLE"):
        extract_document_text(_document(Path("/tmp"), "x.pdf"))


def test_extracts_pdf_through_gate_and_chain(monkeypatch, tmp_path):
    fake = _FakeAnydoc()
    monkeypatch.setattr(doc_extraction, "ANYDOC_AVAILABLE", True)
    monkeypatch.setattr(doc_extraction, "_anydoc", fake)

    progress: list[tuple[str, int]] = []
    text = extract_document_text(_document(tmp_path, "scan.pdf"), on_progress=lambda s, p: progress.append((s, p)))

    assert "พระราชบัญญัติ" in text
    # data, format hint and a callable recognizer all reached anydoc
    data, fmt, recognizer = fake.calls[0]
    assert data == b"fake-bytes" and fmt == "pdf"
    assert callable(recognizer)
    assert ("anydoc_convert", 10) in progress and ("anydoc_done", 30) in progress


def test_mojibake_pdf_is_repaired_by_gate(monkeypatch, tmp_path):
    body = ("พระราชบัญญัติแก้ไขเพิ่มเติมประมวลกฎหมายที่ดิน " * 20)
    mojibake = body.encode("tis-620").decode("mac-roman")
    fake = _FakeAnydoc(markdown=mojibake)
    monkeypatch.setattr(doc_extraction, "ANYDOC_AVAILABLE", True)
    monkeypatch.setattr(doc_extraction, "_anydoc", fake)

    text = extract_document_text(_document(tmp_path, "garbled.pdf"))
    # Gate round-trips the mojibake back to readable Thai.
    assert "พระราชบัญญัติ" in text


def test_empty_usable_pdf_text_raises(monkeypatch, tmp_path):
    fake = _FakeAnydoc(markdown="   ")  # gate empties short meaningless text
    monkeypatch.setattr(doc_extraction, "ANYDOC_AVAILABLE", True)
    monkeypatch.setattr(doc_extraction, "_anydoc", fake)
    with pytest.raises(RuntimeError, match="TEXT_EXTRACTION_EMPTY"):
        extract_document_text(_document(tmp_path, "blank.pdf"))


def test_non_pdf_skips_the_gate(monkeypatch, tmp_path):
    fake = _FakeAnydoc(markdown="plain docx text")
    monkeypatch.setattr(doc_extraction, "ANYDOC_AVAILABLE", True)
    monkeypatch.setattr(doc_extraction, "_anydoc", fake)
    text = extract_document_text(_document(tmp_path, "a.docx"))
    assert text == "plain docx text"
