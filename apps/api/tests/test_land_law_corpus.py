from pathlib import Path

from app.legal_corpus import parse_legal_corpus_metadata
from app.legal_registry import parse_provision_refs, provision_number_matches
from app.legal_resolver import _is_valid
from app.models import LegalInstrument
from datetime import date


CORPUS = Path(__file__).parents[3] / "กรมที่ดิน" / "ผังแสดงการแก้ไข_พระราชบัญญัติแก้ไขเพิ่มเติมประมวลกฎหมายที่ดิน_พ.ศ_2520"


def test_parser_reads_official_header_and_normalizes_work_key():
    path = CORPUS / "05_ฉบับแก้ไข_ครั้งที่_1.txt"
    data = parse_legal_corpus_metadata(path.read_text(encoding="utf-8"), path.name)
    instrument = data["instrument"]
    assert instrument["document_class"] == "amendment"
    assert instrument["source_reference"] == "ป0002-1D-0001-04"
    assert instrument["version_date"] == "1977-09-20"
    assert instrument["legal_work_key"] == "ประมวลกฎหมายที่ดิน"
    assert any(event["target_provision"] == "69 ทวิ" for event in data["change_events"])


def test_parser_handles_title_typo_in_latest_amendment():
    path = CORPUS / "36_ฉบับแก้ไข_ครั้งที่_15.txt"
    data = parse_legal_corpus_metadata(path.read_text(encoding="utf-8"), path.name)
    assert data["instrument"]["legal_work_key"] == "ประมวลกฎหมายที่ดิน"
    assert data["instrument"]["version_date"] == "2019-11-21"
    assert any(event["target_provision"] in {"104", "105", "105 อัฏฐ"} for event in data["change_events"])


def test_parser_distinguishes_latest_consolidation_and_preserves_precise_targets():
    latest = parse_legal_corpus_metadata((CORPUS / "37_ฉบับปรับปรุงล่าสุด.txt").read_text(encoding="utf-8"), "latest.txt")
    amendment_10 = parse_legal_corpus_metadata((CORPUS / "25_ฉบับแก้ไข_ครั้งที่_10.txt").read_text(encoding="utf-8"), "amendment-10.txt")
    amendment_11 = parse_legal_corpus_metadata((CORPUS / "27_ฉบับแก้ไข_ครั้งที่_11.txt").read_text(encoding="utf-8"), "amendment-11.txt")

    assert latest["instrument"]["version_role"] == "latest_consolidated"
    assert any(event["target_provision"] == "57 วรรคสอง" for event in amendment_10["change_events"])
    assert any(event["target_provision"] == "9 ทวิ" for event in amendment_11["change_events"])


def test_provision_parser_keeps_article_filterable_when_a_paragraph_is_named():
    refs = parse_provision_refs("วรรคสองของมาตรา 57 และมาตรา 9/1")
    assert refs == [
        {"kind": "มาตรา", "number": "57", "paragraph": None, "raw": "มาตรา 57"},
        {"kind": "มาตรา", "number": "9/1", "paragraph": None, "raw": "มาตรา 9/1"},
    ]
    paragraph_ref = parse_provision_refs("มาตรา 57 วรรคสอง")[0]
    assert paragraph_ref["number"] == "57"
    assert paragraph_ref["paragraph"] == "วรรคสอง"
    assert provision_number_matches("57 วรรคสอง", paragraph_ref["number"])


def test_historical_resolution_keeps_superseded_expression_before_cutover():
    row = LegalInstrument(status="superseded", effective_from=date(1998, 1, 1), effective_to=date(2019, 11, 21))
    assert _is_valid(row, date(2010, 1, 1)) is True
    assert _is_valid(row, date(2020, 1, 1)) is False


def test_parser_captures_all_provisions_in_a_compound_change_clause():
    # ฉบับที่ 8 inserts both มาตรา 96 ทวิ and มาตรา 96 ตรี in a single clause;
    # missing the และ-joined target dropped the 96 ตรี amendment edge entirely.
    path = CORPUS / "20_ฉบับแก้ไข_ครั้งที่_8.txt"
    data = parse_legal_corpus_metadata(path.read_text(encoding="utf-8"), path.name)
    targets = {event["target_provision"] for event in data["change_events"]}
    assert "96 ทวิ" in targets
    assert "96 ตรี" in targets


def test_parser_captures_every_provision_in_a_long_whitespace_separated_repeal():
    # ฉบับที่ 15 repeals 8 provisions in one clause, only the last joined by
    # และ — the rest are simply listed back to back. A fixed lookahead window
    # or missing ordinal-suffix support previously dropped 7 of the 8.
    path = CORPUS / "36_ฉบับแก้ไข_ครั้งที่_15.txt"
    data = parse_legal_corpus_metadata(path.read_text(encoding="utf-8"), path.name)
    targets = {event["target_provision"] for event in data["change_events"]}
    for expected in ("105", "105 ทวิ", "105 ตรี", "105 จัตวา", "105 เบญจ", "105 ฉ", "105 สัตต", "105 อัฏฐ"):
        assert expected in targets, f"missing {expected} in {targets}"
