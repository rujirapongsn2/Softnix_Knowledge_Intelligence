import {useState} from "react";
import {errorKeysForPatch, parseOptions, summarizeDraftChanges, validateDocumentTypeDraft, withRowId} from "./document-type-validation.mjs";

const emptyDraft = () => ({name: "", description: "", base_document_type: "general", fields: []});
const noErrors = () => ({name: null, fields: {}});
const NO_CHANGES = {added: 0, removed: 0, modified: 0, detailsChanged: false, total: 0};
const blankField = () => withRowId({key: "", label: "", field_type: "text", required: false, fill_mode: "extract", extraction_description: "", review_policy: "evidence", batch_default_allowed: false, help_text: "", options: [], searchable: true, filterable: false, graph_entity_type: "", graph_relationship: ""});
const withFreshRowIds = fields => fields.map(field => withRowId({...field, _uid: undefined}));
const draftFromTemplate = template => ({name: template.name, description: template.description || "", base_document_type: template.base_document_type, fields: withFreshRowIds(template.fields || [])});

export function useDocumentTypeForm() {
  const [editing, setEditing] = useState(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState(emptyDraft);
  const [baseline, setBaseline] = useState(emptyDraft);
  const [fieldErrors, setFieldErrors] = useState(noErrors);
  const [error, setError] = useState("");
  const [lastRemoved, setLastRemoved] = useState(null);
  const [focusRowId, setFocusRowId] = useState(null);

  const isOpen = creating || Boolean(editing);
  const changes = isOpen ? summarizeDraftChanges(baseline, draft) : NO_CHANGES;

  // Drop the errors the user just resolved: one input's, the name's, or all of them when the field list changes.
  const clearErrors = (target, keys = []) => {
    const next = {name: fieldErrors.name, fields: {...fieldErrors.fields}};
    if (target === undefined) { next.name = null; next.fields = {}; }
    else if (target === "name") next.name = null;
    else if (next.fields[target]) {
      const remaining = {...next.fields[target]};
      keys.forEach(key => delete remaining[key]);
      if (Object.keys(remaining).length) next.fields[target] = remaining; else delete next.fields[target];
    }
    setFieldErrors(next);
    if (!next.name && !Object.keys(next.fields).length) setError("");
  };
  const setFields = update => setDraft(current => ({...current, fields: update(current.fields)}));

  const begin = (template, nextEditing, nextCreating) => {
    const loaded = template ? draftFromTemplate(template) : emptyDraft();
    setEditing(nextEditing); setCreating(nextCreating); setDraft(loaded); setBaseline(loaded);
    setError(""); setFieldErrors(noErrors()); setLastRemoved(null); setFocusRowId(null);
  };

  return {
    draft, editing, isOpen, changes, fieldErrors, error, setError, lastRemoved, focusRowId,
    startCreate: () => begin(null, null, true),
    startEdit: template => begin(template, template, false),
    reset: () => begin(null, null, false),
    clearFocusRow: () => setFocusRowId(null),
    setName: name => { clearErrors("name"); setDraft(current => ({...current, name})); },
    setDescription: description => setDraft(current => ({...current, description})),
    setProfile: (base_document_type, profileDefaults) => setDraft(current => ({
      ...current, base_document_type, fields: current.fields.length ? current.fields : withFreshRowIds(profileDefaults[base_document_type] || []),
    })),
    copyProfileDefaults: profileDefaults => { clearErrors(); setDraft(current => ({...current, fields: withFreshRowIds(profileDefaults[current.base_document_type] || [])})); },
    addField: () => { clearErrors(); const row = blankField(); setFields(fields => [...fields, row]); setFocusRowId(row._uid); },
    patchField: (index, patch) => { clearErrors(index, errorKeysForPatch(patch)); setFields(fields => fields.map((field, at) => at === index ? {...field, ...patch} : field)); },
    removeField: index => { clearErrors(); setLastRemoved({field: draft.fields[index], index}); setFields(fields => fields.filter((_, at) => at !== index)); },
    undoRemove: () => {
      if (!lastRemoved) return;
      clearErrors();
      setFields(fields => { const next = [...fields]; next.splice(Math.min(lastRemoved.index, next.length), 0, lastRemoved.field); return next; });
      setLastRemoved(null);
    },
    validate: () => {
      const {errors, count} = validateDocumentTypeDraft(draft);
      setFieldErrors(count ? errors : noErrors());
      return count;
    },
    toPayload: () => ({...draft, fields: draft.fields.map(({options_text, _uid, ...field}) => ({...field, options: parseOptions({...field, options_text})}))}),
  };
}
