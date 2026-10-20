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
    assert.deepEqual(statusTip(t, {status: "completed"}), {meaning: t("status.completed.help")});
    assert.deepEqual(Object.keys(failed), ["meaning", "action", "detail"]);
    assert.equal(statusTip(t, {status: "mystery"}), null);
    assert.ok(errorGuide(t, "NOT_A_KNOWN_CODE").action);
  });

  const section = (tip, kind) => tip.sections.find(item => item.kind === kind);

  test(`${lang}: the review hint is titled with the badge, says what search through the API and MCP does, and how to resolve it`, () => {
    const attention = metadataReviewTip(t, {metadata_status: "needs_attention"});
    const unverified = metadataReviewTip(t, {metadata_status: "unverified"});
    assert.equal(attention.title, t("documents.badge.needsAttention"));
    assert.equal(unverified.title, t("documents.badge.unverified"));
    for (const [tip, status] of [[attention, "needs_attention"], [unverified, "unverified"]]) {
      assert.equal(tip.meaning, t(`hint.metadata.${status}`));
      assert.deepEqual(tip.sections.map(item => item.kind), ["effect", "steps"], `${status}: nothing waits, so no fields section`);
      assert.equal(section(tip, "effect").heading, t("hint.metadata.section.effect"));
      assert.match(section(tip, "effect").heading, /MCP/);
      assert.equal(section(tip, "steps").heading, t(`hint.metadata.section.steps.${status}`));
      assert.ok(section(tip, "steps").ordered && section(tip, "steps").items.length >= 2);
    }
    assert.deepEqual(section(attention, "effect").items, ["content", "usable", "blocked", "missing"].map(key => t(`hint.metadata.needs_attention.effect.${key}`)));
    assert.deepEqual(section(unverified, "effect").items, ["content", "values", "answers"].map(key => t(`hint.metadata.unverified.effect.${key}`)));
    assert.match(section(unverified, "effect").items[1], /"unverified"/);
    assert.equal(section(attention, "steps").items[1], t("hint.metadata.needs_attention.step.resolve"));
    assert.notDeepEqual(section(attention, "steps").items, section(unverified, "steps").items);
  });

  test(`${lang}: the review hint lists the waiting fields by label, most urgent first, and cuts a long list short`, () => {
    const document = {metadata_status: "needs_attention", metadata_template_fields: [{key: "party", label: "Party"}, {key: "other"}, {key: "done", label: "Done"}],
      metadata_field_states: {party: "unverified", other: "blocked", done: "verified"}};
    assert.deepEqual(section(metadataReviewTip(t, document), "fields"), {kind: "fields", heading: t("hint.metadata.section.fields"),
      items: [t("hint.metadata.pending.fix", {fields: "other"}), t("hint.metadata.pending.check", {fields: "Party"})]});
    const unverified = metadataReviewTip(t, {...document, metadata_status: "unverified", metadata_field_states: {party: "unverified", done: "verified"}});
    assert.deepEqual(section(unverified, "fields").items, [t("hint.metadata.pending.check", {fields: "Party"})]);
    const keys = ["a", "b", "c", "d", "e", "f"];
    const many = metadataReviewTip(t, {metadata_status: "needs_attention", metadata_template_fields: keys.map(key => ({key, label: key.toUpperCase()})),
      metadata_field_states: Object.fromEntries(keys.map(key => [key, "missing"]))});
    const more = lang === "en" ? "A, B, C, D and 2 more" : "A, B, C, D และอีก 2 ฟิลด์";
    assert.deepEqual(section(many, "fields").items, [t("hint.metadata.pending.fix", {fields: more})]);
  });

  test(`${lang}: every status filter option has a hint`, () => {
    for (const value of ["all", "metadata_review", "metadata_attention", "queued", "extracting", "indexing", "completed", "failed", "ocr_required", "deleted"]) assert.ok(statusFilterTip(t, value).meaning, value);
  });

  test(`${lang}: users, services, jobs, legal and review statuses all have a hint`, () => {
    for (const role of ["user", "manager", "admin"]) assert.ok(roleTip(t, role).meaning, role);
    assert.equal(roleTip(t, "guest"), null);
    assert.ok(userStatusTip(t, true).meaning && userStatusTip(t, false).meaning && serviceTip(t, "ready").meaning && serviceTip(t, "unavailable").meaning);
    assert.notEqual(serviceTip(t, "not_configured").meaning, serviceTip(t, "unavailable").meaning, "an unset optional service is not described as down");
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
