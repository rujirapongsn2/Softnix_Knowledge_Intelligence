import assert from "node:assert/strict";
import test from "node:test";
import {validateDocumentTypeDraft} from "../src/document-type-validation.mjs";

const field = (overrides = {}) => ({key: "issuer", label: "Issuer", field_type: "text", fill_mode: "manual", extraction_description: "", options: [], ...overrides});
const draft = (fields, name = "Policy") => ({name, fields});

test("accepts a complete draft", () => {
  assert.equal(validateDocumentTypeDraft(draft([field(), field({key: "tags", label: "Tags", field_type: "multi_select", options_text: "a, b"})])).count, 0);
});

test("flags a multi-select field without options at that field", () => {
  const {errors, count} = validateDocumentTypeDraft(draft([field(), field({key: "myfield2", label: "My", field_type: "multi_select", options_text: " , "})]));
  assert.equal(count, 1);
  assert.deepEqual(errors.fields[1], {options: "required"});
  assert.equal(errors.fields[0], undefined);
});

test("falls back to stored options when the options text was never edited", () => {
  assert.equal(validateDocumentTypeDraft(draft([field({field_type: "select", options: ["x"]})])).count, 0);
});

test("flags duplicate options, bad keys, missing labels and duplicate keys", () => {
  const {errors} = validateDocumentTypeDraft(draft([
    field({key: "Bad Key", label: ""}),
    field({key: "same", field_type: "select", options_text: "a, a"}),
    field({key: "same"}),
  ]));
  assert.deepEqual(errors.fields[0], {key: "format", label: "required"});
  assert.deepEqual(errors.fields[1], {key: "duplicate", options: "duplicate"});
  assert.deepEqual(errors.fields[2], {key: "duplicate"});
});

test("requires an extraction instruction only for extract fields and a document type name", () => {
  const {errors} = validateDocumentTypeDraft(draft([field({fill_mode: "extract", extraction_description: "  "})], " "));
  assert.equal(errors.name, "required");
  assert.deepEqual(errors.fields[0], {extraction: "required"});
});

import {summarizeDraftChanges, withRowId} from "../src/document-type-validation.mjs";

test("summarizes unsaved changes against the loaded version", () => {
  const a = withRowId(field({key: "a"})), b = withRowId(field({key: "b", help_text: null}));
  const baseline = {name: "T", description: "", base_document_type: "general", fields: [a, b]};
  assert.equal(summarizeDraftChanges(baseline, {...baseline, fields: [a, b]}).total, 0);
  assert.deepEqual(summarizeDraftChanges(baseline, {...baseline, fields: [a]}), {added: 0, removed: 1, modified: 0, detailsChanged: false, total: 1});
  const added = withRowId(field({key: "c"}));
  const result = summarizeDraftChanges(baseline, {...baseline, name: "T2", fields: [{...a, label: "Changed"}, {...b, help_text: ""}, added]});
  assert.deepEqual(result, {added: 1, removed: 0, modified: 1, detailsChanged: true, total: 3});
});

test("treats a never-edited options list and an equal edited one as unchanged", () => {
  const f = withRowId(field({key: "x", field_type: "select", options: ["a", "b"]}));
  const baseline = {name: "T", description: "", base_document_type: "general", fields: [f]};
  assert.equal(summarizeDraftChanges(baseline, {...baseline, fields: [{...f, options_text: "a, b"}]}).total, 0);
});
