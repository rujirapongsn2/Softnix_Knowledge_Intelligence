import test from "node:test";
import assert from "node:assert/strict";
import {translations} from "../src/translations.js";
import {DOCUMENT_ERROR_CODES, errorGuide, metadataReviewTip, statusFilterTip, statusTip} from "../src/hints.mjs";

for (const lang of ["en", "th"]) {
  const t = (key, vars = {}) => {
    assert.ok(key in translations[lang], `${lang} is missing ${key}`);
    return translations[lang][key].replace(/\{(\w+)\}/g, (_, name) => vars[name]);
  };

  test(`${lang}: every document error code explains itself and what to do`, () => {
    for (const code of DOCUMENT_ERROR_CODES) assert.ok(errorGuide(t, code).title && errorGuide(t, code).action, code);
  });

  test(`${lang}: a failed document shows its error guide and raw detail, a healthy one only its meaning`, () => {
    const failed = statusTip(t, {status: "failed", error_code: "RETRIEVAL_ENGINE_BUSY", error_message: "engine said busy"});
    assert.equal(failed.meaning, errorGuide(t, "RETRIEVAL_ENGINE_BUSY").title);
    assert.equal(failed.detail, "RETRIEVAL_ENGINE_BUSY · engine said busy");
    assert.deepEqual(Object.keys(statusTip(t, {status: "completed"})), ["meaning"]);
    assert.equal(statusTip(t, {status: "mystery"}), null);
    assert.ok(errorGuide(t, "NOT_A_KNOWN_CODE").action);
  });

  test(`${lang}: the review hint names the fields waiting by label`, () => {
    const document = {metadata_template_fields: [{key: "party", label: "Party"}], metadata_pending_fields: ["party", "other"]};
    assert.match(metadataReviewTip(t, document).pending, /Party, other/);
    assert.equal(metadataReviewTip(t, {}).pending, null);
  });

  test(`${lang}: every status filter option has a hint`, () => {
    for (const value of ["all", "metadata_review", "queued", "extracting", "indexing", "completed", "failed", "ocr_required", "deleted"]) assert.ok(statusFilterTip(t, value).meaning, value);
  });
}
