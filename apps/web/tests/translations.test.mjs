import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import {translations} from "../src/translations.js";

const source = path.join(import.meta.dirname, "../src");

test("Thai and English define exactly the same keys", () => {
  const en = Object.keys(translations.en);
  const th = new Set(Object.keys(translations.th));
  assert.deepEqual(en.filter(key => !th.has(key)), [], "keys missing from Thai");
  const english = new Set(en);
  assert.deepEqual([...th].filter(key => !english.has(key)), [], "keys missing from English");
});

test("every literal t(\"key\") used by the app has a translation", () => {
  const used = new Set();
  for (const file of fs.readdirSync(source).filter(name => /\.(jsx|js|mjs)$/.test(name) && name !== "translations.js")) {
    for (const match of fs.readFileSync(path.join(source, file), "utf8").matchAll(/\bt\(\s*"([A-Za-z0-9_.]+)"/g)) used.add(match[1]);
  }
  assert.deepEqual([...used].filter(key => !(key in translations.en)), []);
});
