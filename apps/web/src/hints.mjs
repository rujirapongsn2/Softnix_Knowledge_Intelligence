// What a hover hint says. Each builder returns {meaning, pending?, action?, detail?} or null.

export const ERROR_CODES = [
  "RETRIEVAL_ENGINE_DUPLICATE", "RETRIEVAL_ENGINE_REJECTED", "RETRIEVAL_ENGINE_UNAVAILABLE", "RETRIEVAL_ENGINE_BUDGET_EXHAUSTED",
  "RETRIEVAL_ENGINE_BUSY", "RETRIEVAL_ENGINE_TIMEOUT", "OCR_REQUIRED", "OCR_CHAIN_FAILED", "TEXT_EXTRACTION_EMPTY", "TEXT_EXTRACTION_FAILED",
  "FILE_TYPE_NOT_SUPPORTED", "OPENROUTER_UNAVAILABLE", "OPENROUTER_EMBEDDING_INVALID_RESPONSE", "OPENROUTER_EMBEDDING_DIMENSION_MISMATCH",
  "OPENROUTER_LLM_INVALID_RESPONSE", "EXTERNAL_OCR_NOT_CONFIGURED", "EXTERNAL_OCR_UNAVAILABLE", "EXTERNAL_OCR_REJECTED",
  "EXTERNAL_OCR_TIMEOUT", "EXTERNAL_OCR_EMPTY_RESULT", "EXTERNAL_OCR_INVALID_RESPONSE",
  "REMOTE_INDEX_PURGE_FAILED", "LEGAL_GRAPH_REBUILD_FAILED", "METADATA_EXTRACTION_FAILED", "METADATA_EXTRACTION_INVALID_RESPONSE",
  "METADATA_TEXT_NOT_READY", "OPENROUTER_API_KEY_NOT_CONFIGURED", "DOCUMENT_DELETED",
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

export const metadataReviewTip = (t, document) => {
  const labels = new Map((document.metadata_template_fields || []).map(field => [field.key, field.label || field.key]));
  const pending = (document.metadata_pending_fields || []).map(key => labels.get(key) || key);
  return {meaning: t("hint.metadata.needs_review"), pending: pending.length ? t("hint.metadata.pending", {fields: pending.join(", ")}) : null, action: t("hint.metadata.needs_review.action")};
};

export const statusFilterTip = (t, value) => {
  if (value === "all") return {meaning: t("hint.filter.all")};
  if (value === "metadata_review") return {meaning: t("hint.filter.metadata_review"), action: t("hint.metadata.needs_review.action")};
  if (value === "deleted") return {meaning: t("hint.filter.deleted")};
  return STATUSES_WITH_HELP.includes(value) ? {meaning: t(`status.${value}.help`)} : null;
};

export const kbStatusTip = (t, status) => ["active", "draft", "disabled"].includes(status) ? {meaning: t(`hint.kb.${status}`)} : null;

const oneOf = (list, value) => list.includes(value) ? value : null;

export const roleTip = (t, role) => oneOf(["user", "manager", "admin"], role) && {meaning: t(`hint.role.${role}`)};

export const userStatusTip = (t, isActive) => ({meaning: t(isActive ? "hint.userStatus.active" : "hint.userStatus.inactive")});

export const serviceTip = (t, isReady) => ({meaning: t(isReady ? "hint.service.up" : "hint.service.down")});

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
