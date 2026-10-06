import test from "node:test";
import assert from "node:assert/strict";
import {translations} from "../src/translations.js";
import {ERROR_CODES, errorGuide, fieldTypeHelp, jobTip, legalStatusTip, metadataReviewTip, reviewStatusTip, roleTip, serviceTip, statusFilterTip, statusTip, userStatusTip} from "../src/hints.mjs";

for (const lang of ["en", "th"]) {
  const t = (key, vars = {}) => {
    assert.ok(key in translations[lang], `${lang} is missing ${key}`);
    return translations[lang][key].replace(/\{(\w+)\}/g, (_, name) => vars[name]);
  };

  test(`${lang}: every document error code explains itself and what to do`, () => {
    for (const code of ERROR_CODES) assert.ok(errorGuide(t, code).title && errorGuide(t, code).action, code);
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

  test(`${lang}: users, services, jobs, legal and review statuses all have a hint`, () => {
    for (const role of ["user", "manager", "admin"]) assert.ok(roleTip(t, role).meaning, role);
    assert.equal(roleTip(t, "guest"), null);
    assert.ok(userStatusTip(t, true).meaning && userStatusTip(t, false).meaning && serviceTip(t, true).meaning && serviceTip(t, false).meaning);
    for (const status of ["in_force", "amended", "not_yet_effective", "unknown", "superseded", "repealed", "anything"]) assert.ok(legalStatusTip(t, status).meaning, status);
    for (const status of ["verified", "suggested", "rejected", "unreviewed", undefined]) assert.ok(reviewStatusTip(t, status).meaning, String(status));
  });

  test(`${lang}: text list and multi-select each explain the other's difference with an example`, () => {
    const example = lang === "en" ? /For example/ : /เช่น/;
    for (const type of ["text_list", "multi_select", "select"]) assert.match(fieldTypeHelp(t, type), example, type);
    assert.match(fieldTypeHelp(t, "text_list"), lang === "en" ? /no preset options/ : /ไม่มีตัวเลือกกำหนดไว้/);
    assert.match(fieldTypeHelp(t, "multi_select"), lang === "en" ? /from the options/ : /จากตัวเลือกที่กำหนดไว้/);
    assert.equal(fieldTypeHelp(t, "date"), undefined);
  });

  test(`${lang}: a failed job explains its error code and keeps the raw detail`, () => {
    const tip = jobTip(t, {status: "failed", error_code: "REMOTE_INDEX_PURGE_FAILED", error_message: "engine busy"});
    assert.equal(tip.meaning, errorGuide(t, "REMOTE_INDEX_PURGE_FAILED").title);
    assert.equal(tip.detail, "REMOTE_INDEX_PURGE_FAILED \u00b7 engine busy");
    assert.deepEqual(Object.keys(jobTip(t, {status: "running"})), ["meaning"]);
    assert.equal(jobTip(t, {status: "weird"}), null);
  });
}
