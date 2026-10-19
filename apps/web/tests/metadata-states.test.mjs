import test from "node:test";
import assert from "node:assert/strict";
import {fieldsByUrgency, metadataBadge, waitingFields} from "../src/metadata-states.mjs";

const fields = ["a", "b", "c", "d", "manual"].map(key => ({key}));
const states = {a: "auto_verified", b: "unverified", c: "blocked", d: "missing"};

test("fields a person must fix come first, then values in use but unchecked, then finished ones", () => {
  assert.deepEqual(fieldsByUrgency(fields, states).map(field => field.key), ["c", "d", "b", "a"]);
});

test("only fields that still ask something of a person are waiting; fields the API gave no state are left out", () => {
  assert.deepEqual(waitingFields(fields, states).map(field => field.key), ["c", "d", "b"]);
  assert.deepEqual(waitingFields(fields, {a: "verified", b: "not_found"}), []);
});

test("a document's badge tells fixing apart from checking", () => {
  assert.equal(metadataBadge("needs_attention").tone, "attention");
  assert.equal(metadataBadge("unverified").tone, "unverified");
  for (const status of ["complete", "queued", "running", "failed", "not_started"]) assert.equal(metadataBadge(status), null, status);
});
