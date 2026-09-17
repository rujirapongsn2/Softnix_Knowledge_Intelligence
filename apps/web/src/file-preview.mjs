const FILE_PREVIEW_HEADING_RE = /^[ \t]*(มาตรา|ข้อ|หมวด|ส่วนที่|บทเฉพาะกาล|บทนิยาม)\s*([0-9๐-๙]+(?:\/[0-9๐-๙]+)?)?\s*(ทวิ|ตรี|จัตวา|เบญจ)?[^\n]*/gm;

export const provisionTocLabel = item => {
  if (!item || typeof item !== "object") return "";
  const heading = String(item.heading || item.title || "").trim();
  if (heading) return heading.slice(0, 120);
  const number = item.number || item.article_number || item.label;
  if (number == null || number === "") {
    const text = String(item.text || "").trim();
    return text ? text.slice(0, 64) : "";
  }
  const raw = String(number).trim();
  if (/^(มาตรา|ข้อ|หมวด|ส่วนที่|Article|Section|Clause)/i.test(raw)) return raw.slice(0, 120);
  const kind = String(item.kind || "article").toLowerCase();
  const prefix = kind === "section" || kind === "หมวด" ? "หมวด"
    : kind === "clause" || kind === "ข้อ" ? "ข้อ"
    : kind === "part" || kind === "ส่วนที่" ? "ส่วนที่"
    : "มาตรา";
  return `${prefix} ${raw}`.slice(0, 120);
};

const findHintIndex = (text, hints, searchFrom = 0) => {
  for (const hint of hints) {
    const needle = String(hint || "").trim();
    if (!needle) continue;
    let index = text.indexOf(needle, searchFrom);
    if (index < 0 && searchFrom > 0) index = text.indexOf(needle);
    if (index >= 0) return index;
  }
  return -1;
};

export const buildFilePreviewSections = (rawText, legalMetadata, t) => {
  const text = String(rawText || "");
  const legal = legalMetadata && typeof legalMetadata === "object" ? legalMetadata : {};
  const candidates = [];

  const provisions = Array.isArray(legal.provisions) ? legal.provisions : [];
  for (const item of provisions) {
    const label = provisionTocLabel(item);
    if (!label) continue;
    const number = item.number || item.article_number;
    const hints = [item.evidence_quote, item.heading, item.title, number != null ? `มาตรา ${number}` : "", number != null ? `ข้อ ${number}` : "", number != null ? `หมวด ${number}` : "", label].filter(Boolean);
    candidates.push({label, hints});
  }

  if (!candidates.length) {
    const articles = Array.isArray(legal.articles) ? legal.articles : [];
    for (const item of articles) {
      const label = provisionTocLabel(item);
      if (!label) continue;
      const number = item.article_number || item.number;
      const hints = [item.evidence_quote, item.heading, item.title, number != null ? `มาตรา ${number}` : "", label].filter(Boolean);
      candidates.push({label, hints});
    }
  }

  if (!candidates.length && text) {
    FILE_PREVIEW_HEADING_RE.lastIndex = 0;
    let match;
    while ((match = FILE_PREVIEW_HEADING_RE.exec(text)) !== null) {
      const label = match[0].trim().replace(/\s+/g, " ").slice(0, 120);
      if (label) candidates.push({label, hints: [match[0].trim()], index: match.index});
    }
  }

  if (!candidates.length) {
    return [{id: "fp-sec-0", label: t("documentPreview.filePreview.tocContent"), body: text}];
  }

  const located = [];
  let searchFrom = 0;
  candidates.forEach((candidate, index) => {
    const start = candidate.index != null ? candidate.index : findHintIndex(text, candidate.hints, searchFrom);
    if (start < 0) return;
    located.push({id: `fp-sec-${index}`, label: candidate.label, start});
    searchFrom = start + 1;
  });

  located.sort((left, right) => left.start - right.start || left.id.localeCompare(right.id));
  const deduped = [];
  for (const row of located) {
    if (deduped.length && deduped[deduped.length - 1].start === row.start) continue;
    deduped.push(row);
  }

  if (!deduped.length) {
    return [{id: "fp-sec-0", label: t("documentPreview.filePreview.tocContent"), body: text}];
  }
  if (deduped[0].start > 0) {
    deduped.unshift({id: "fp-sec-preamble", label: t("documentPreview.filePreview.tocPreamble"), start: 0});
  }
  return deduped.map((row, index) => {
    const end = index + 1 < deduped.length ? deduped[index + 1].start : text.length;
    return {...row, body: text.slice(row.start, end)};
  });
};

const FILE_PREVIEW_EMPTY_MARKERS = new Set(["", "—", "-", "–", "unknown", "null", "undefined", "n/a", "na"]);

export const isFilePreviewEmptyValue = value => {
  if (value == null) return true;
  const text = String(value).trim();
  return !text || FILE_PREVIEW_EMPTY_MARKERS.has(text.toLowerCase());
};

export const pickFilePreviewValue = (...candidates) => {
  for (const candidate of candidates) {
    if (!isFilePreviewEmptyValue(candidate)) return typeof candidate === "string" ? candidate.trim() : candidate;
  }
  return null;
};

export const pickDocumentMetadataValue = (documentMetadata, keys) => {
  if (!documentMetadata || typeof documentMetadata !== "object") return null;
  for (const key of keys) {
    const exact = documentMetadata[key];
    if (!isFilePreviewEmptyValue(exact)) return typeof exact === "string" ? exact.trim() : exact;
  }
  const normalizedWanted = new Set(keys.map(key => String(key).trim().toLowerCase()));
  for (const [key, value] of Object.entries(documentMetadata)) {
    if (!normalizedWanted.has(String(key).trim().toLowerCase()) || isFilePreviewEmptyValue(value)) continue;
    return typeof value === "string" ? value.trim() : value;
  }
  return null;
};

export const mapFilePreviewClassLabel = (labels, value) => {
  if (isFilePreviewEmptyValue(value)) return null;
  const key = String(value).trim();
  return labels.class?.[key] || key;
};
