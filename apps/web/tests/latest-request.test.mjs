import test from "node:test";
import assert from "node:assert/strict";
import {latestRequest} from "../src/latest-request.mjs";

test("an answer to an older request is ignored once a newer request has started", async () => {
  const requests = latestRequest();
  const shown = [];
  const load = async (kb, delay) => {
    const isCurrent = requests.begin();
    const report = await new Promise(resolve => setTimeout(() => resolve(`report for ${kb}`), delay));
    if (isCurrent()) shown.push(report);
  };
  await Promise.all([load("kb-a", 20), load("kb-b", 0)]);
  assert.deepEqual(shown, ["report for kb-b"]);
});
