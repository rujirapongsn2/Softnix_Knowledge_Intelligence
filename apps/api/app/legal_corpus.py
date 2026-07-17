"""Deterministic parser for official Thai legal corpus headers and change clauses.

The parser is deliberately conservative: it only promotes values written in the
source header or explicit amendment clauses.  LLM extraction may add candidates,
but must not replace these provenance-bearing fields.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .legal_registry import _to_arabic_digits, normalize_family_key, parse_thai_date


_HEADER_KEYS = ("ชื่อในผัง", "ชื่อกฎหมาย", "รหัสฉบับ", "วันที่ในผัง", "แหล่งข้อมูล")
_CHANGE_RE = re.compile(
    r"(?P<clause>(?:มาตรา|ข้อ)\s*[0-9๐-๙]+(?:\s*(?:ทวิ|ตรี|จัตวา|เบญจ))?[^\n]{0,420}?"
    r"(?:ให้ยกเลิกความใน|ให้ยกเลิก|ให้เพิ่ม|เพิ่มความใน|ให้ใช้ความต่อไปนี้แทน)[^\n]{0,700})",
    re.IGNORECASE,
)
_TARGET_RE = re.compile(r"(?:ใน|เป็น|แทน|ยกเลิก)\s*(?:มาตรา|ข้อ)\s*([0-9๐-๙]+(?:\s*(?:ทวิ|ตรี|จัตวา|เบญจ))?)", re.IGNORECASE)


@dataclass(frozen=True)
class LegalCorpusHeader:
    display_title: str | None = None
    official_title: str | None = None
    source_reference: str | None = None
    version_date: str | None = None
    source_uri: str | None = None
    document_class: str = "unknown"
    legal_work_key: str | None = None
    version_label: str | None = None
    official_number: str | None = None


def _clean(value: str | None) -> str:
    value = (value or "").replace("เเก้ไข", "แก้ไข").replace("เเก้", "แก้")
    return re.sub(r"\s+", " ", value).strip()


def parse_header(text: str) -> LegalCorpusHeader:
    values: dict[str, str] = {}
    for line in (text or "").splitlines()[:30]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = _clean(key), _clean(value)
        if key in _HEADER_KEYS and value:
            values[key] = value
    display = values.get("ชื่อในผัง")
    title = values.get("ชื่อกฎหมาย") or display
    compact = _clean(display or title).casefold()
    if "ฉบับหลัก" in compact:
        document_class = "main"
    elif "ฉบับปรับปรุง" in compact or "รวม" in compact:
        document_class = "consolidated"
    elif "ฉบับแก้ไข" in compact or "แก้ไขเพิ่มเติม" in compact:
        document_class = "amendment"
    else:
        document_class = "unknown"
    _, version_label, _ = normalize_family_key(title or "")
    # Keep the base work stable across amendment titles and title typos.
    normalized_title = _clean(title or "")
    work = re.sub(r"^พระราชบัญญัติ\s*แก้ไขเพิ่มเติม\s*", "", normalized_title)
    work = re.sub(r"^พระราชบัญญัติ\s*", "", work)
    work = re.sub(r"\(\s*ฉบับที่\s*[0-9๐-๙]+\s*\)", "", work)
    work = re.sub(r"(?:พ\.?\s*ศ\.?|ค\.?\s*ศ\.?)\s*[0-9๐-๙]{4}", "", work)
    work = _clean(work) or None
    number = None
    match = re.search(r"\(\s*ฉบับที่\s*([0-9๐-๙]+)\s*\)", normalized_title)
    if match:
        number = _to_arabic_digits(match.group(1))
    version_date = parse_thai_date(values.get("วันที่ในผัง"))
    return LegalCorpusHeader(
        display_title=display,
        official_title=title,
        source_reference=values.get("รหัสฉบับ"),
        version_date=version_date.isoformat() if version_date else None,
        source_uri=values.get("แหล่งข้อมูล"),
        document_class=document_class,
        legal_work_key=work,
        version_label=version_label,
        official_number=number,
    )


def parse_change_events(text: str) -> list[dict]:
    events: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for match in _CHANGE_RE.finditer(text or ""):
        quote = _clean(match.group("clause"))[:5000]
        provision_match = re.match(r"(?:มาตรา|ข้อ)\s*([0-9๐-๙]+(?:\s*(?:ทวิ|ตรี|จัตวา|เบญจ))?)", quote, re.I)
        if not provision_match:
            continue
        provision = _to_arabic_digits(provision_match.group(1)).strip()
        lower = quote.casefold()
        action = "repeal" if "ให้ยกเลิก" in lower and "ให้ใช้ความต่อไปนี้แทน" not in lower else "replace"
        if "ให้เพิ่ม" in lower or "เพิ่มความใน" in lower:
            action = "insert"
        target_match = _TARGET_RE.search(quote)
        target = _to_arabic_digits(target_match.group(1)).strip() if target_match else provision
        key = (action, provision, target)
        if key in seen:
            continue
        seen.add(key)
        events.append({"action": action, "provision_kind": "มาตรา" if "มาตรา" in quote else "ข้อ",
                       "provision_number": provision, "target_provision": target,
                       "evidence_quote": quote, "origin": "legal_schema", "review_status": "verified",
                       "confidence": 1.0})
    return events


def parse_legal_corpus_metadata(text: str, fallback_title: str = "") -> dict:
    header = parse_header(text)
    if not header.official_title:
        header = LegalCorpusHeader(official_title=_clean(fallback_title))
    events = parse_change_events(text)
    instrument = asdict(header)
    # Avoid leaking dataclass implementation details into the public schema.
    instrument = {key: value for key, value in instrument.items() if value not in (None, "")}
    return {"schema_version": 2, "instrument": instrument, "change_events": events,
            "amendments": events, "references": [], "provenance": {"extractor": "legal_corpus_parser", "evidence_required": True}}
