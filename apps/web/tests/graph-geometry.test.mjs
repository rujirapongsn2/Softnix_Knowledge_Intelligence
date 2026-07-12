import assert from "node:assert/strict";
import test from "node:test";
import {connectionHandles} from "../src/graph-geometry.mjs";

const node = (x, y) => ({position: {x, y}});

test("connects horizontal neighbors at their facing sides", () => {
  assert.deepEqual(connectionHandles(node(0, 0), node(200, 20)), {sourceHandle: "right", targetHandle: "left"});
  assert.deepEqual(connectionHandles(node(200, 20), node(0, 0)), {sourceHandle: "left", targetHandle: "right"});
});

test("connects vertical neighbors at their facing sides", () => {
  assert.deepEqual(connectionHandles(node(0, 0), node(20, 200)), {sourceHandle: "bottom", targetHandle: "top"});
  assert.deepEqual(connectionHandles(node(20, 200), node(0, 0)), {sourceHandle: "top", targetHandle: "bottom"});
});

test("uses the dominant axis for diagonal neighbors", () => {
  assert.deepEqual(connectionHandles(node(0, 0), node(80, 160)), {sourceHandle: "bottom", targetHandle: "top"});
  assert.deepEqual(connectionHandles(node(0, 0), node(-160, 80)), {sourceHandle: "left", targetHandle: "right"});
});

test("does not invent handles before both nodes exist", () => {
  assert.deepEqual(connectionHandles(node(0, 0), undefined), {});
});
