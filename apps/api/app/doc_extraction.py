"""anydoc-backed document extraction — the fast path for every format anydoc
supports (doc/docx/odt/rtf/epub/pdf/ppt(x)/xls(x)/ods/odp/csv), with the
OCR chain handling scanned or garbled Thai PDF pages.

This module is the single entry point the worker calls. ``anydoc`` is an
optional dependency: when the wheel is missing the caller receives
``RuntimeError("ANYDOC_UNAVAILABLE")`` and falls back to the legacy
MarkItDown path (removed in the cleanup phase once the wheel ships in the
image).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .config import get_settings
from .ocr_chain import build_recognizer

ProgressCallback = Callable[[str, int], None]

#: Extensions anydoc handles natively (everything except HTML family).
_ANYDOC_EXTENSIONS = {
    ".doc", ".docx", ".docm", ".odt", ".rtf", ".epub", ".pdf",
    ".ppt", ".pps", ".pot", ".pptx", ".pptm", ".ppsx", ".ppsm",
    ".xls", ".xlsx", ".xlsm", ".xlsb", ".ods", ".odp", ".csv",
}

try:  # pragma: no cover - exercised implicitly by the import-time check
    import anydoc as _anydoc
    ANYDOC_AVAILABLE = True
except ImportError:  # pragma: no cover
    _anydoc = None
    ANYDOC_AVAILABLE = False


def anydoc_supports(path: str | Path) -> bool:
    """True when anydoc can convert this file (excludes .html/.htm)."""
    return Path(path).suffix.lower() in _ANYDOC_EXTENSIONS


def extract_document_text(document, *, on_progress: ProgressCallback | None = None) -> str:
    """Convert one uploaded file to Markdown via anydoc + OCR chain.

    Raises:
        RuntimeError: ``ANYDOC_UNAVAILABLE`` when the wheel is missing, or
            the OCR chain's ``OCR_CHAIN_FAILED: ...`` when every engine
            failed on a page that needed OCR.
    """
    if not ANYDOC_AVAILABLE:
        raise RuntimeError("ANYDOC_UNAVAILABLE")

    from .services import _repair_or_flag_pdf_text

    path = Path(document.storage_path)
    data = path.read_bytes()
    fmt = _anydoc.format_from_path(path)

    if on_progress:
        on_progress("anydoc_convert", 10)

    # The pyo3 binding accepts a plain callable (image, page) -> str, so
    # hand it the bound method rather than the chain object itself.
    recognizer = build_recognizer(on_progress=on_progress)
    markdown = _anydoc.to_markdown_with_ocr(data, fmt, recognizer.recognize)

    if path.suffix.lower() == ".pdf":
        gate = _repair_or_flag_pdf_text(markdown)
        if not gate:
            # The whole document OCR'd into nothing usable (or the layer was
            # empty and no engine recovered it) — surface a plain failure.
            raise RuntimeError("TEXT_EXTRACTION_EMPTY")
        markdown = gate

    if on_progress:
        on_progress("anydoc_done", 30)

    return markdown
