// Client-side validation for the document type editor. It returns error codes
// (not text) per field so the form can show a translated, actionable message
// next to the input that needs fixing.

export const FIELD_KEY_PATTERN = /^[a-z][a-z0-9_]*$/;
const CHOICE_TYPES = new Set(["select", "multi_select"]);

export const parseOptions = field => (
  field.options_text === undefined ? field.options || [] : field.options_text.split(",").map(item => item.trim()).filter(Boolean)
);

export function validateDocumentTypeDraft(draft) {
  const errors = {name: null, fields: {}};
  if (!draft.name.trim()) errors.name = "required";
  const keyCounts = new Map();
  draft.fields.forEach(field => keyCounts.set(field.key, (keyCounts.get(field.key) || 0) + 1));
  draft.fields.forEach((field, index) => {
    const fieldErrors = {};
    if (!field.key.trim()) fieldErrors.key = "required";
    else if (!FIELD_KEY_PATTERN.test(field.key)) fieldErrors.key = "format";
    else if (keyCounts.get(field.key) > 1) fieldErrors.key = "duplicate";
    if (!field.label.trim()) fieldErrors.label = "required";
    if (CHOICE_TYPES.has(field.field_type)) {
      const options = parseOptions(field);
      if (!options.length) fieldErrors.options = "required";
      else if (new Set(options).size !== options.length) fieldErrors.options = "duplicate";
    }
    if (field.fill_mode === "extract" && !(field.extraction_description || "").trim()) fieldErrors.extraction = "required";
    if (Object.keys(fieldErrors).length) errors.fields[index] = fieldErrors;
  });
  const count = (errors.name ? 1 : 0) + Object.values(errors.fields).reduce((total, item) => total + Object.keys(item).length, 0);
  return {errors, count};
}

// Rows get a client-only id so React can track them across removals and so
// unsaved changes can be diffed against the version that was loaded.
let rowIdCounter = 0;
export const withRowId = field => (field._uid ? field : {...field, _uid: `row-${++rowIdCounter}`});

const TEXT_KEYS = ["help_text", "extraction_description", "graph_entity_type", "graph_relationship"];
const comparableField = ({_uid, options_text, ...field}) => {
  const normalized = {...field, options: parseOptions({...field, options_text})};
  TEXT_KEYS.forEach(key => { normalized[key] = normalized[key] || ""; });
  return JSON.stringify(normalized, Object.keys(normalized).sort());
};

// Compare an edited draft with the version it started from.
export function summarizeDraftChanges(baseline, draft) {
  const before = new Map(baseline.fields.map(field => [field._uid, comparableField(field)]));
  const after = new Map(draft.fields.map(field => [field._uid, comparableField(field)]));
  let added = 0, removed = 0, modified = 0;
  after.forEach((value, id) => { if (!before.has(id)) added += 1; else if (before.get(id) !== value) modified += 1; });
  before.forEach((_, id) => { if (!after.has(id)) removed += 1; });
  const detailsChanged = ["name", "description", "base_document_type"].some(key => (baseline[key] || "") !== (draft[key] || ""));
  return {added, removed, modified, detailsChanged, total: added + removed + modified + (detailsChanged ? 1 : 0)};
}

// Which error a field edit resolves, so the message disappears as soon as the input is touched.
const ERROR_RESOLVED_BY = {key: "key", label: "label", options_text: "options", field_type: "options", extraction_description: "extraction", fill_mode: "extraction"};
export const errorKeysForPatch = patch => Object.keys(patch).map(name => ERROR_RESOLVED_BY[name]).filter(Boolean);
