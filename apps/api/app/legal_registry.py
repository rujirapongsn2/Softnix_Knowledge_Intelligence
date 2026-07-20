"""Deterministic legal-registry helpers: kind/authority classification, family and
date normalization, and the status resolver. No LLM calls happen in this module."""
import re
from datetime import date, datetime

from sqlalchemy.orm import Session

from .models import LegalInstrument, LegalInstrumentRelation

AUTHORITY_LEVELS: dict[str, int] = {
    "constitution": 100,
    "act": 90,
    "royal_decree": 80,
    "ministerial_regulation": 70,
    "notification": 60,
    "rule": 50,
    "circular": 40,
    "guideline": 30,
    "resolution": 30,
    "contract": 30,
    "faq": 20,
    "other": 20,
}
VALID_KINDS = frozenset(AUTHORITY_LEVELS)

# Checked in order; a title matching an earlier, more specific pattern wins
# (e.g. a royal decree title must not be classified as a ministerial one).
_KIND_THAI_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("constitution", ("รัฐธรรมนูญ",)),
    ("act", ("พระราชบัญญัติ", "พ.ร.บ.", "พรบ.", "พระราชกำหนด", "พ.ร.ก.", "พรก.")),
    ("royal_decree", ("พระราชกฤษฎีกา", "พ.ร.ฎ.")),
    ("ministerial_regulation", ("กฎกระทรวง",)),
    ("notification", ("ประกาศ",)),
    ("rule", ("ระเบียบ", "ข้อบังคับ")),
    ("circular", ("หนังสือเวียน", "หนังสือตอบข้อหารือ")),
    ("guideline", ("แนวปฏิบัติ", "คู่มือ", "มาตรฐาน")),
    ("resolution", ("มติ",)),
]
_KIND_ENGLISH_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("constitution", ("constitution",)),
    ("act", ("act",)),
    ("royal_decree", ("royal decree",)),
    ("ministerial_regulation", ("ministerial regulation",)),
    ("notification", ("notification",)),
    ("rule", ("regulation", "rule")),
    ("circular", ("circular",)),
    ("guideline", ("guideline", "manual", "standard")),
    ("resolution", ("resolution",)),
]

_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def _to_arabic_digits(text: str) -> str:
    return text.translate(_THAI_DIGITS)


def classify_kind(title: str, extracted_kind: str | None = None) -> str:
    """Rule-based Thai legal instrument classification; falls back to a validated LLM hint."""
    value = (title or "").strip()
    for kind, patterns in _KIND_THAI_PATTERNS:
        if any(pattern in value for pattern in patterns):
            return kind
    lowered = value.casefold()
    for kind, patterns in _KIND_ENGLISH_PATTERNS:
        if any(re.search(rf"\b{re.escape(pattern)}\b", lowered) for pattern in patterns):
            return kind
    normalized_extracted = str(extracted_kind or "").strip().casefold().replace(" ", "_")
    if normalized_extracted in VALID_KINDS:
        return normalized_extracted
    return "other"


_VERSION_PATTERN = re.compile(r"\(\s*ฉบับที่\s*([0-9๐-๙]+)\s*\)")
_BE_YEAR_PATTERN = re.compile(r"(?:พ\.?\s*ศ\.?|พุทธศักราช)\s*([0-9๐-๙]{4})")
_CE_YEAR_PATTERN = re.compile(r"(?:ค\.?\s*ศ\.?|B\.?E\.?|A\.?D\.?)\s*([0-9๐-๙]{4})", re.IGNORECASE)


def normalize_family_key(title: str) -> tuple[str, str | None, int | None]:
    """Return (base_title_key, version_label, enacted_year_ce) so amendments of the
    same instrument group into one family regardless of their version suffix."""
    value = (title or "").strip()
    version_label = None
    version_match = _VERSION_PATTERN.search(value)
    if version_match:
        version_label = f"ฉบับที่ {_to_arabic_digits(version_match.group(1))}"
        value = value[:version_match.start()] + value[version_match.end():]
    enacted_year = None
    be_match = _BE_YEAR_PATTERN.search(value)
    if be_match:
        enacted_year = int(_to_arabic_digits(be_match.group(1))) - 543
        value = value[:be_match.start()] + value[be_match.end():]
    else:
        ce_match = _CE_YEAR_PATTERN.search(value)
        if ce_match:
            enacted_year = int(_to_arabic_digits(ce_match.group(1)))
            value = value[:ce_match.start()] + value[ce_match.end():]
    normalized = re.sub(r"\s+", " ", value).strip().casefold()
    return normalized, version_label, enacted_year


_THAI_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4, "พฤษภาคม": 5, "มิถุนายน": 6,
    "กรกฎาคม": 7, "สิงหาคม": 8, "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
}
_THAI_DATE_PATTERN = re.compile(r"([0-9๐-๙]{1,2})\s*(" + "|".join(_THAI_MONTHS) + r")\s*([0-9๐-๙]{4})")


def parse_thai_date(value) -> date | None:
    """Parse a Buddhist- or Christian-era Thai date, an ISO date, or pass a date through."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    match = _THAI_DATE_PATTERN.search(text)
    if match:
        day = int(_to_arabic_digits(match.group(1)))
        month = _THAI_MONTHS[match.group(2)]
        year = int(_to_arabic_digits(match.group(3)))
        if year > 2400:
            year -= 543
        try:
            return date(year, month, day)
        except ValueError:
            return None
    try:
        return date.fromisoformat(_to_arabic_digits(text)[:10])
    except ValueError:
        return None


_PROVISION_PATTERN = re.compile(
    r"(มาตรา|ข้อ|หมวด|ส่วนที่|section|article|clause)\s*([0-9๐-๙]+(?:/[0-9๐-๙]+)?)\s*(ทวิ|ตรี|จัตวา|เบญจ)?(?:\s*(วรรค\s*(?:[0-9๐-๙]+|หนึ่ง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า|สิบ)))?",
    re.IGNORECASE,
)
_PROVISION_KIND_LABELS = {"section": "มาตรา", "article": "มาตรา", "clause": "ข้อ"}


def parse_provision_refs(text: str) -> list[dict]:
    """Find มาตรา/ข้อ/หมวด references (Thai or Arabic numerals) in free text."""
    results = []
    for match in _PROVISION_PATTERN.finditer(text or ""):
        raw_kind, number, suffix, paragraph = match.group(1), match.group(2), match.group(3), match.group(4)
        kind = _PROVISION_KIND_LABELS.get(raw_kind.casefold(), raw_kind)
        normalized_number = _to_arabic_digits(number) + (f" {suffix}" if suffix else "")
        results.append(
            {
                "kind": kind,
                "number": normalized_number.strip(),
                "paragraph": paragraph or None,
                "raw": match.group(0).strip(),
            }
        )
    return results


_PROVISION_PREFIX = re.compile(r"^(มาตรา|ข้อ|section|article|clause)\s*", re.IGNORECASE)


def _normalize_provision_number(value: str) -> str:
    value = re.sub(r"\s+", "", _PROVISION_PREFIX.sub("", (value or "").strip())).casefold()
    # Thai consolidation texts use both ``9 ทวิ`` and ``9/1`` for the same
    # provision.  Normalize the ordinal suffix only at comparison time so the
    # registry retains the source wording for citations and auditability.
    value = re.sub(r"([0-9๐-๙]+)ทวิ$", r"\1/1", value)
    value = re.sub(r"([0-9๐-๙]+)ตรี$", r"\1/2", value)
    value = re.sub(r"([0-9๐-๙]+)จัตวา$", r"\1/3", value)
    value = re.sub(r"([0-9๐-๙]+)เบญจ$", r"\1/4", value)
    value = re.sub(r"([0-9๐-๙]+)ฉ$", r"\1/5", value)
    value = re.sub(r"([0-9๐-๙]+)สัตต$", r"\1/6", value)
    value = re.sub(r"([0-9๐-๙]+)อัฏฐ$", r"\1/7", value)
    value = re.sub(r"([0-9๐-๙]+)นว$", r"\1/8", value)
    value = re.sub(r"([0-9๐-๙]+)ทศ$", r"\1/9", value)
    return value


def provision_number_matches(target_provision: str | None, section_number: str | None) -> bool:
    """Compare a free-text provision reference (e.g. 'ข้อ 5') against a chunk's
    normalized section_number (e.g. '5'), ignoring the kind prefix and whitespace."""
    if not target_provision or not section_number:
        return False
    target, section = _normalize_provision_number(target_provision), _normalize_provision_number(section_number)
    # A relation targeting a paragraph still overrides the article body for
    # version selection; callers needing paragraph-specific text retain the
    # full target value for provenance display.
    return target == section or target.startswith(section + "วรรค")


_WHOLE_INSTRUMENT_REPEAL_STATUS = {"REPEALS": "repealed", "SUPERSEDES": "superseded"}


def resolve_instrument_statuses(db: Session, knowledge_base_id: str) -> dict[str, int]:
    """Deterministically resolve in_force/amended/repealed/superseded status for one
    Knowledge Base's legal registry. Only status_source == 'resolver' rows are touched;
    a manual override always wins and this function never calls an LLM."""
    instruments = db.query(LegalInstrument).filter_by(knowledge_base_id=knowledge_base_id).all()
    by_id = {row.id: row for row in instruments}
    today = date.today()
    changed = 0

    def set_status(row: LegalInstrument, status: str, reason: str) -> None:
        nonlocal changed
        if row.status_source == "manual":
            return
        if row.status != status or row.status_reason != reason:
            changed += 1
        row.status, row.status_reason = status, reason

    for row in instruments:
        if row.status_source == "manual":
            continue
        if row.effective_from and row.effective_from > today:
            set_status(row, "not_yet_effective", "effective_from is in the future")
        elif row.effective_from:
            set_status(row, "in_force", "effective_from has passed")
        else:
            set_status(row, "unknown", "no effective date extracted")

    relations = db.query(LegalInstrumentRelation).filter(
        LegalInstrumentRelation.knowledge_base_id == knowledge_base_id,
        LegalInstrumentRelation.review_status == "verified",
        LegalInstrumentRelation.target_instrument_id.is_not(None),
    ).all()
    for relation in relations:
        target = by_id.get(relation.target_instrument_id)
        source = by_id.get(relation.source_instrument_id)
        if not target or not source:
            continue
        # A provision-level edge (target_provision set) narrows to one article and
        # must not flip the whole instrument's status; Phase 3 conflict detection
        # handles that finer-grained case using this same relation row.
        if relation.target_provision:
            continue
        if relation.relation in _WHOLE_INSTRUMENT_REPEAL_STATUS:
            new_status = _WHOLE_INSTRUMENT_REPEAL_STATUS[relation.relation]
            set_status(target, new_status, f"{new_status} by {source.id} via verified {relation.relation}")
            if not target.effective_to:
                target.effective_to = source.effective_from or target.effective_to
        elif relation.relation == "AMENDS" and target.status not in {"repealed", "superseded"}:
            set_status(target, "amended", f"amended by {source.id} via verified AMENDS")

    def _family_sort_key(item: LegalInstrument) -> date:
        # A missing effective_from must not sort as the oldest possible date
        # when enacted_year is known, or a re-enactment whose effective date
        # failed to extract would rank before a genuinely older instrument.
        if item.effective_from:
            return item.effective_from
        if item.enacted_year:
            return date(item.enacted_year, 1, 1)
        return date.min

    families: dict[str, list[LegalInstrument]] = {}
    for row in instruments:
        if row.family_id:
            families.setdefault(row.family_id, []).append(row)
    for members in families.values():
        # Only instruments without a "(ฉบับที่ N)" version suffix are treated as
        # standalone full re-enactments; amendment acts are handled by the AMENDS
        # edge above and never marked superseded purely by family ordering.
        full_versions = sorted((item for item in members if not item.version_label), key=_family_sort_key)
        # "current" is the newest full version already in effect. A future-dated
        # re-enactment must not block superseding older versions that are
        # already in force -- it is excluded from consideration, not treated as
        # disqualifying the whole family.
        eligible = [item for item in full_versions if not (item.effective_from and item.effective_from > today)]
        if len(eligible) < 2:
            continue
        current = eligible[-1]
        for older in full_versions:
            if older is current:
                continue
            if older.effective_from and older.effective_from > today:
                continue  # not yet effective itself; leave its baseline status
            if older.status_source == "manual" or older.status in {"repealed", "superseded"}:
                continue
            set_status(older, "superseded", f"newer full version in family: {current.id}")
            if not older.effective_to:
                older.effective_to = current.effective_from

    db.flush()
    return {"instruments": len(instruments), "changed": changed}
