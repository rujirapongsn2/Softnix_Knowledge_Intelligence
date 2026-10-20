// What a hover hint says. Each builder returns {meaning, title?, sections?, action?, detail?} or null;
// a section is {kind, heading, items, ordered?}.
import {fieldReview, metadataBadge, waitingFields} from "./metadata-states.mjs";

export const ERROR_CODES = [
  "RETRIEVAL_ENGINE_DUPLICATE", "RETRIEVAL_ENGINE_REJECTED", "RETRIEVAL_ENGINE_UNAVAILABLE", "RETRIEVAL_ENGINE_BUDGET_EXHAUSTED",
  "RETRIEVAL_ENGINE_BUSY", "RETRIEVAL_ENGINE_TIMEOUT", "OCR_REQUIRED", "OCR_CHAIN_FAILED", "TEXT_EXTRACTION_EMPTY", "TEXT_EXTRACTION_FAILED",
  "FILE_TYPE_NOT_SUPPORTED", "LLM_UNAVAILABLE", "LLM_AUTH_FAILED", "LLM_CREDITS_EXHAUSTED", "LLM_REQUEST_REJECTED",
  "EMBEDDING_NOT_CONFIGURED", "EMBEDDING_UNAVAILABLE", "EMBEDDING_AUTH_FAILED", "EMBEDDING_CREDITS_EXHAUSTED", "EMBEDDING_REQUEST_TOO_LARGE", "EMBEDDING_REQUEST_REJECTED", "EMBEDDING_INVALID_RESPONSE", "EMBEDDING_DIMENSION_MISMATCH",
  "LLM_INVALID_RESPONSE", "EXTERNAL_OCR_NOT_CONFIGURED", "EXTERNAL_OCR_UNAVAILABLE", "EXTERNAL_OCR_REJECTED",
  "EXTERNAL_OCR_TIMEOUT", "EXTERNAL_OCR_EMPTY_RESULT", "EXTERNAL_OCR_INVALID_RESPONSE",
  "REMOTE_INDEX_PURGE_FAILED", "LEGAL_GRAPH_REBUILD_FAILED", "METADATA_EXTRACTION_FAILED", "METADATA_EXTRACTION_INVALID_RESPONSE",
  "METADATA_TEXT_NOT_READY", "LLM_API_KEY_NOT_CONFIGURED", "DOCUMENT_DELETED", "WORKER_LOST",
];

const STATUSES_WITH_HELP = ["queued", "extracting", "indexing", "completed", "failed", "ocr_required", "deleted"];

export const errorGuide = (t, code) => ERROR_CODES.includes(code)
  ? {title: t(`documentError.${code}.title`), action: t(`documentError.${code}.hint`)}
  : {title: t("hint.error.generic.title"), action: t("hint.error.generic.action")};

export const statusTip = (t, document) => {
  if (!STATUSES_WITH_HELP.includes(document.status)) return null;
  const tip = {meaning: t(`status.${document.status}.help`)};
  if (document.status === "failed" || document.status === "ocr_required") {
    const guide = document.error_code ? errorGuide(t, document.error_code) : {action: t("hint.error.generic.action")};
    Object.assign(tip, {meaning: document.error_code ? guide.title : tip.meaning, action: guide.action, detail: [document.error_code, document.error_message].filter(Boolean).join(" · ") || null});
  }
  return tip;
};

const MAX_NAMED_FIELDS = 4;
const METADATA_TIP = {
  needs_attention: {effect: ["content", "usable", "blocked", "missing"], steps: ["open", "resolve", "optional"]},
  unverified: {effect: ["content", "values", "answers"], steps: ["open", "resolve", "counted"]},
};

const fieldList = (t, names) => names.length > MAX_NAMED_FIELDS
  ? t("hint.metadata.fields.more", {fields: names.slice(0, MAX_NAMED_FIELDS).join(", "), count: names.length - MAX_NAMED_FIELDS})
  : names.join(", ");

export const metadataReviewTip = (t, document) => {
  const status = document.metadata_status === "needs_attention" ? "needs_attention" : "unverified";
  const states = document.metadata_field_states || {};
  const waiting = waitingFields(document.metadata_template_fields || [], states);
  const names = review => waiting.filter(field => fieldReview(states[field.key]) === review).map(field => field.label || field.key);
  const fieldRows = [["needs_attention", "hint.metadata.pending.fix"], ["unverified", "hint.metadata.pending.check"]]
    .filter(([review]) => names(review).length).map(([review, key]) => t(key, {fields: fieldList(t, names(review))}));
  return {
    title: t(metadataBadge(status).labelKey),
    meaning: t(`hint.metadata.${status}`),
    sections: [
      {kind: "effect", heading: t("hint.metadata.section.effect"), items: METADATA_TIP[status].effect.map(key => t(`hint.metadata.${status}.effect.${key}`))},
      fieldRows.length && {kind: "fields", heading: t("hint.metadata.section.fields"), items: fieldRows},
      {kind: "steps", heading: t(`hint.metadata.section.steps.${status}`), items: METADATA_TIP[status].steps.map(key => t(`hint.metadata.${status}.step.${key}`)), ordered: true},
    ].filter(Boolean),
  };
};

export const statusFilterTip = (t, value) => {
  if (value === "all") return {meaning: t("hint.filter.all")};
  if (value === "metadata_review" || value === "metadata_attention") return {meaning: t(`hint.filter.${value}`), action: t("hint.metadata.review.action")};
  if (value === "deleted") return {meaning: t("hint.filter.deleted")};
  return STATUSES_WITH_HELP.includes(value) ? {meaning: t(`status.${value}.help`)} : null;
};

export const kbStatusTip = (t, status) => ["active", "draft", "disabled"].includes(status) ? {meaning: t(`hint.kb.${status}`)} : null;

const oneOf = (list, value) => list.includes(value) ? value : null;

export const roleTip = (t, role) => oneOf(["user", "manager", "admin"], role) && {meaning: t(`hint.role.${role}`)};

export const userStatusTip = (t, isActive) => ({meaning: t(isActive ? "hint.userStatus.active" : "hint.userStatus.inactive")});

export const serviceTip = (t, state) => ({meaning: t(state === "ready" ? "hint.service.up" : state === "not_configured" ? "hint.service.notConfigured" : "hint.service.down")});

export const jobTip = (t, job) => {
  if (!oneOf(["queued", "running", "completed", "failed", "cancelled"], job.status)) return null;
  const tip = {meaning: t(`hint.job.${job.status}`)};
  if (job.error_code) Object.assign(tip, {meaning: errorGuide(t, job.error_code).title, action: errorGuide(t, job.error_code).action, detail: [job.error_code, job.error_message].filter(Boolean).join(" \u00b7 ")});
  return tip;
};

export const legalStatusTip = (t, status) => ({meaning: t(`hint.legalStatus.${oneOf(["in_force", "amended", "not_yet_effective", "superseded", "repealed"], status) || "unknown"}`)});

export const reviewStatusTip = (t, status) => ({meaning: t(`hint.reviewStatus.${oneOf(["verified", "suggested", "rejected"], status) || "unreviewed"}`)});

const FIELD_TYPE_HELP = {text: "textHelp", textarea: "textareaHelp", text_list: "textListHelp", select: "selectHelp", multi_select: "multiSelectHelp"};

export const fieldTypeHelp = (t, fieldType) => FIELD_TYPE_HELP[fieldType] ? t(`documentType.editor.fieldType.${FIELD_TYPE_HELP[fieldType]}`) : undefined;
