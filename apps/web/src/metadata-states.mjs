// The API computes each extracted field's review state (metadata_field_states); this only says what each state asks of a person.
// "inconsistent" only appears on a next action: the value disagrees with the document's legal metadata.
export const FIELD_STATE_REVIEW = {
  verified: "complete", auto_verified: "complete", not_found: "complete",
  unverified: "unverified", unchecked: "unverified", blocked: "needs_attention", missing: "needs_attention", inconsistent: "needs_attention",
};
const URGENCY = ["needs_attention", "unverified", "complete"];

export const fieldReview = state => FIELD_STATE_REVIEW[state] || "unverified";

export const fieldsByUrgency = (fields, states = {}) => fields
  .filter(field => field.key in states)
  .map((field, index) => ({field, index, review: fieldReview(states[field.key])}))
  .sort((a, b) => URGENCY.indexOf(a.review) - URGENCY.indexOf(b.review) || a.index - b.index)
  .map(({field}) => field);

export const waitingFields = (fields, states = {}) => fieldsByUrgency(fields, states).filter(field => fieldReview(states[field.key]) !== "complete");

// Document list filter value -> the API's metadata_status filter.
export const METADATA_STATUS_FILTERS = {metadata_review: "needs_review", metadata_attention: "needs_attention"};

export const metadataBadge = status => status === "needs_attention" ? {labelKey: "documents.badge.needsAttention", tone: "attention"}
  : status === "unverified" ? {labelKey: "documents.badge.unverified", tone: "unverified"} : null;
