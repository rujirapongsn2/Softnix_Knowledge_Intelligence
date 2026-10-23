import assert from "node:assert/strict";
import test from "node:test";
import {axisMax, barLabel, barTitle, groupTotals, labelEvery, stackBars} from "../src/error-timeline.mjs";

const groups = ["retrieval_engine", "ai_provider"];
const counts = (retrieval_engine = 0, ai_provider = 0) => ({retrieval_engine, ai_provider});
const timeline = {groups, series: [
  {start: "2026-10-21T00:00:00+07:00", failed: counts(2, 1), retried: counts(5, 0)},
  {start: "2026-10-22T00:00:00+07:00", failed: counts(0, 0), retried: counts(0, 3)},
]};

test("stacks failed errors by group, and retried ones above them only when asked", () => {
  const failedOnly = stackBars(timeline, false);
  assert.deepEqual(failedOnly.bars.map(bar => bar.total), [3, 0]);
  assert.deepEqual(failedOnly.bars[0].segments, [
    {group: "retrieval_engine", outcome: "failed", value: 2, from: 0, to: 2},
    {group: "ai_provider", outcome: "failed", value: 1, from: 2, to: 3},
  ]);
  const withRetried = stackBars(timeline, true);
  assert.deepEqual(withRetried.bars[0].segments.at(-1), {group: "retrieval_engine", outcome: "retried", value: 5, from: 3, to: 8});
  assert.equal(withRetried.max, 10, "the axis tops out at a round value above the tallest bar");
});

test("rounds the axis up to 1, 2, 2.5 or 5 times a power of ten", () => {
  assert.deepEqual([0, 1, 3, 7, 12, 25, 26, 89].map(axisMax), [1, 1, 5, 10, 20, 25, 50, 100]);
});

test("totals each group over the period", () => {
  assert.deepEqual(groupTotals(timeline, false), {retrieval_engine: 2, ai_provider: 1});
  assert.deepEqual(groupTotals(timeline, true), {retrieval_engine: 7, ai_provider: 4});
});

test("labels fewer bars as the period grows", () => {
  assert.deepEqual([7, 24, 30, 90].map(labelEvery), [1, 3, 5, 15]);
});

test("labels hourly bars by time and daily bars by date", () => {
  assert.equal(barLabel("2026-10-23T14:00:00+07:00", "hour"), "14:00");
  assert.equal(barTitle("2026-10-23T14:00:00+07:00", "hour"), "23/10 14:00");
  assert.equal(barLabel("2026-10-23T00:00:00+07:00", "day"), "23/10");
});
