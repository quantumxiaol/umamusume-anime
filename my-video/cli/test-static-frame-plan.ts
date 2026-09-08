import assert from "node:assert/strict";
import { staticFramePlan } from "../src/lib/static-frame-plan";
import type { z } from "zod";
import type { TimelineSchema } from "../src/lib/types";

const timeline = {
  elements: [
    { startMs: 0, endMs: 5100, imageUrl: "a" },
    { startMs: 5100, endMs: 6000, imageUrl: "b" },
  ],
  text: [{ startMs: 3, endMs: 4999, id: "a", subtitleJa: "hello" }],
  audio: [],
} as unknown as z.infer<typeof TimelineSchema>;
const plan = staticFramePlan(timeline, 60, 60, 0, 419);
const rep = (f: number) => plan.representatives[plan.indices[f]];
assert.equal(plan.indices.length, 420);
assert.equal(rep(0), rep(59)); // Static intro.
assert.notEqual(rep(60), rep(61)); // Fractional subtitle entry.
assert.equal(rep(100), rep(200)); // Stable caption hold.
assert.notEqual(rep(359), rep(360)); // Caption exit into the gap.
assert.notEqual(rep(365), rep(366)); // Background boundary.
const slice = staticFramePlan(timeline, 60, 60, 120, 200);
assert.deepEqual(slice.representatives, [120]); // Chunk begins mid-hold.
const animated = {
  ...timeline,
  elements: [
    {
      ...timeline.elements[0],
      animations: [
        { type: "scale", startMs: 0, endMs: 5000, from: 1, to: 1.1 },
      ],
    },
  ],
} as z.infer<typeof TimelineSchema>;
assert.equal(
  staticFramePlan(animated, 60, 60, 100, 200).representatives.length,
  101,
);
console.log(
  "static frame plan: boundaries, fractional timing, holds, chunks, animation fallback passed",
);
