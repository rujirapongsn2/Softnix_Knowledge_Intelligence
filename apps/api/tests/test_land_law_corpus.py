from pathlib import Path

from app.legal_corpus import parse_legal_corpus_metadata
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


def test_historical_resolution_keeps_superseded_expression_before_cutover():
    row = LegalInstrument(status="superseded", effective_from=date(1998, 1, 1), effective_to=date(2019, 11, 21))
    assert _is_valid(row, date(2010, 1, 1)) is True
    assert _is_valid(row, date(2020, 1, 1)) is False
