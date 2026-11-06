"""Query-focused passage selection without dropping short clauses or their exceptions."""
from collections import Counter
from difflib import SequenceMatcher
import math
import re
import unicodedata

PASSAGE_CHARS = 6000
ANSWER_CONTEXT_CHARS = 36000
_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def context_budgets(lengths: list[int]) -> list[int]:
    """Share context fairly: retain short clauses, and never omit later sources."""
    budgets = [0] * len(lengths)
    pending = set(range(len(lengths)))
    remaining = ANSWER_CONTEXT_CHARS
    while pending:
        share = min(PASSAGE_CHARS, remaining // len(pending))
        short = {i for i in pending if lengths[i] <= share}
        if not short:
            for i in pending:
                budgets[i] = share
            break
        for i in short:
            budgets[i] = lengths[i]
            remaining -= lengths[i]
        pending -= short
    return budgets


def normalized_text(value: str) -> str:
    return re.sub(r"[^\w\u0e00-\u0e7f]", "", unicodedata.normalize("NFKC", value).translate(_DIGITS).casefold().replace("_", ""))


def passage_query(query: str, title: str) -> str:
    """Remove literal title phrases so boilerplate cannot outrank the requested facts."""
    value = normalized_text(query)
    blocks = SequenceMatcher(None, value, normalized_text(title), autojunk=False).get_matching_blocks()
    for block in reversed(blocks):
        if block.size >= 12:
            value = value[:block.a] + " " + value[block.a + block.size:]
    return value if len(value.replace(" ", "")) >= 12 else query


def rank_passages(query: str, passages: list[str]) -> list[int]:
    """Rank Thai substrings as well as space-delimited text, with corpus-local IDF."""
    value = normalized_text(query)
    terms = {value[i:i + 4] for i in range(max(0, len(value) - 3))}
    texts = [normalized_text(p) for p in passages]
    frequencies = Counter(term for term in terms for p in texts if term in p)
    weights = {term: math.log(1 + (len(texts) + 1) / (count + 1)) for term, count in frequencies.items()}
    scores = [sum(weight for term, weight in weights.items() if term in p) / math.sqrt(1 + len(p) / 1000)
              for p in texts]
    return sorted(range(len(passages)), key=lambda i: (-scores[i], i))


def select_passage(query: str, content: str, max_chars: int = PASSAGE_CHARS) -> str:
    if len(content) <= max_chars:
        return content
    # Overlapping windows preserve local context. Prefer paragraph boundaries
    # where possible and retain source order in the selected output.
    # Each window starts inside the previous one, so a paragraph cut cannot leave unreachable text.
    width = min(1800, max_chars)
    windows = []
    start = 0
    while True:
        end = min(len(content), start + width)
        boundary = content.rfind("\n\n", start + width // 2, end)
        if boundary > start and end < len(content):
            end = boundary
        windows.append((start, end))
        if end >= len(content):
            break
        start = max(start + 1, end - 400)
    order = rank_passages(query, [content[a:b] for a, b in windows])
    selected = []
    for index in order:
        start, end = windows[index]
        spans = sorted(selected + [(start, end)])
        merged = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        if sum(b - a for a, b in merged) + 5 * max(0, len(merged) - 1) <= max_chars:
            selected = merged
    return "\n[…]\n".join(content[a:b] for a, b in selected)


def named_document_ids(query: str, identities: list[tuple[str, str, str]]) -> list[str] | None:
    """Resolve an explicitly named provincial instrument, without guessing a version.

    identities contain (id, title/filename, opening text). Publication-year
    filenames may differ from the instrument year printed in the opening.
    Multiple provinces and weak/ambiguous title matches retain normal search.
    """
    provinces = re.findall(r"จังหวัด([ก-๙]+)", query)
    if len(set(provinces)) != 1:
        return None
    if re.search(r"เปรียบเทียบ|เทียบกับ|ระหว่าง|ต่างกัน|แตกต่าง|compare|versus", query, re.IGNORECASE):
        return None
    value = normalized_text(query)
    years = set(re.findall(r"พศ(\d{4})", value))
    if len(years) != 1 or not any(normalized_text(word) in value for word in ("ข้อกำหนด", "ข้อบังคับ", "ประกาศ", "ระเบียบ")):
        return None
    versions = set(re.findall(r"ฉบับที่[\s_-]*(\d+)", query.translate(_DIGITS)))
    if len(versions) > 1:
        return None
    province = normalized_text("จังหวัด" + provinces[0])
    matches = []
    for doc_id, title, opening in identities:
        name = normalized_text(title)
        identity = name + normalized_text(opening)
        if province not in name or not years.issubset(set(re.findall(r"พศ(\d{4})", identity))):
            continue
        doc_versions = set(re.findall(r"ฉบับที่[\s_-]*(\d+)", title.translate(_DIGITS)))
        if versions != doc_versions:
            continue
        longest = SequenceMatcher(None, value, name, autojunk=False).find_longest_match().size
        if longest >= 24:
            matches.append((longest, doc_id))
    if not matches:
        return None
    best = max(score for score, _ in matches)
    selected = [doc_id for score, doc_id in matches if score == best]
    return selected if len(selected) <= 5 else None
