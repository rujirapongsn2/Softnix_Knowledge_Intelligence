"""PDF text-layer repair gate: codec recovery + Thai-ratio OCR fallback.

Covers the broken-font PDF case where extraction produces mac-roman glyphs
of TIS-620 Thai text: the layer must be repaired deterministically when a
known wrong-decode fits, and routed to OCR when it does not.
"""
from app.services import (
    _repair_or_flag_pdf_text,
    _thai_ratio,
    _try_codec_recovery,
)


THAI_PARAGRAPH = (
    "พระราชบัญญัติแก้ไขเพิ่มเติมประมวลกฎหมายที่ดิน ฉบับที่ ๙ พ.ศ. ๒๕๔๓ "
    "เป็นกฎหมายที่แก้ไขเพิ่มเติมการได้มาซึ่งที่ดินของคนต่างด้าว "
    "โดยกำหนดหลักเกณฑ์และเงื่อนไขตามที่กำหนดในกฎกระทรวง "
) * 3


def test_thai_ratio_detects_clean_thai():
    assert _thai_ratio(THAI_PARAGRAPH) > 0.5


def test_codec_recovery_repairs_tis620_read_as_mac_roman():
    mojibake = THAI_PARAGRAPH.encode("tis-620").decode("mac-roman")
    assert _thai_ratio(mojibake) < 0.02, "fixture should be mojibake"

    recovered = _try_codec_recovery(mojibake)
    assert recovered is not None
    assert _thai_ratio(recovered) > 0.5
    # Round-trip repair is lossless for the pure-Thai body.
    assert "พระราชบัญญัติ" in recovered


def test_codec_recovery_leaves_clean_text_alone():
    assert _try_codec_recovery(THAI_PARAGRAPH) is None


def test_gate_returns_recovered_text():
    mojibake = THAI_PARAGRAPH.encode("tis-620").decode("mac-roman")
    gated = _repair_or_flag_pdf_text(mojibake)
    assert _thai_ratio(gated) > 0.5
    assert "พระราชบัญญัติ" in gated


def test_gate_passes_clean_thai_through():
    assert _repair_or_flag_pdf_text(THAI_PARAGRAPH) == THAI_PARAGRAPH


def test_gate_passes_english_pdf_through():
    english = ("This agreement is governed by the laws of the Kingdom. " * 10)
    assert _repair_or_flag_pdf_text(english) == english


def test_gate_empties_unrecoverable_garbled_layer():
    # Long extended-glyph gibberish that no codec pair repairs -> OCR required.
    garbage = "".join(chr(0x2010 + (i % 40)) for i in range(500))
    assert _repair_or_flag_pdf_text(garbage) == ""


def test_gate_keeps_short_extracts():
    # Under the minimum length the bare meaningful-character rule applies
    # (same threshold as the pre-gate behaviour).
    short_ok = "Customer Portal runs on application server APP-01 today."
    assert len(short_ok) < 200
    assert _repair_or_flag_pdf_text(short_ok) == short_ok
    assert _repair_or_flag_pdf_text("///,,,___") == ""
