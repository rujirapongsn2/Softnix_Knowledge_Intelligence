import assert from "node:assert/strict";
import test from "node:test";
import {buildFilePreviewSections, pickDocumentMetadataValue, provisionTocLabel} from "../src/file-preview.mjs";

const t = key => ({
  "documentPreview.filePreview.tocContent": "Content",
  "documentPreview.filePreview.tocPreamble": "Preamble",
})[key] || key;

test("builds ordered sections from evidence-backed legal provisions", () => {
  const text = "คำนำ\nมาตรา 1 ให้ใช้บังคับ\nมาตรา 2 ให้ยกเลิก";
  const sections = buildFilePreviewSections(text, {provisions: [
    {number: "1", evidence_quote: "มาตรา 1 ให้ใช้บังคับ"},
    {number: "2", evidence_quote: "มาตรา 2 ให้ยกเลิก"},
  ]}, t);
  assert.deepEqual(sections.map(section => section.label), ["Preamble", "มาตรา 1", "มาตรา 2"]);
  assert.equal(sections.map(section => section.body).join(""), text);
});

test("falls back to Thai headings when legal metadata is unavailable", () => {
  const text = "บทนำ\nข้อ ๑ ขอบเขต\nข้อ ๒ หน้าที่";
  const sections = buildFilePreviewSections(text, null, t);
  assert.deepEqual(sections.map(section => section.label), ["Preamble", "ข้อ ๑ ขอบเขต", "ข้อ ๒ หน้าที่"]);
  assert.equal(sections.map(section => section.body).join(""), text);
});

test("keeps all text in one section when no heading can be identified", () => {
  const text = "Research notes without a legal heading.";
  assert.deepEqual(buildFilePreviewSections(text, {}, t), [{id: "fp-sec-0", label: "Content", body: text}]);
});

test("uses labels and case-insensitive metadata aliases safely", () => {
  assert.equal(provisionTocLabel({kind: "clause", number: "3"}), "ข้อ 3");
  assert.equal(pickDocumentMetadataValue({LEGAL_STATUS: "active"}, ["legal_status"]), "active");
  assert.equal(pickDocumentMetadataValue({legal_status: "unknown"}, ["legal_status"]), null);
});
