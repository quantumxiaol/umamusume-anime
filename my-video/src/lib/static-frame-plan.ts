import type { z } from "zod";
import type { TimelineSchema } from "./types";
import { storySubtitleTheme } from "./subtitle-theme";

// Conservative frame equivalence for AIVideo: only the steady state of known
// static layers is reused. Fractional Sequence boundaries remain unrounded.
export function staticFramePlan(
  timeline: z.infer<typeof TimelineSchema>,
  fps: number,
  intro: number,
  start: number,
  end: number,
) {
  const representatives: number[] = [];
  const indices: number[] = [];
  let previous = "";
  for (let frame = start; frame <= end; frame++) {
    const layers: string[] = [frame < intro ? "intro" : "body"];
    let dynamic = false;
    for (const [i, item] of timeline.elements.entries()) {
      const from = (item.startMs * fps) / 1000 + intro;
      if (frame < from || frame >= (item.endMs * fps) / 1000 + intro) continue;
      layers.push(`image:${i}`);
      if (
        item.animations?.length ||
        (item.enterTransition && item.enterTransition !== "none") ||
        (item.exitTransition && item.exitTransition !== "none")
      )
        dynamic = true;
    }
    for (const [i, item] of timeline.text.entries()) {
      const from = (item.startMs * fps) / 1000 + intro;
      const duration = ((item.endMs - item.startMs) * fps) / 1000;
      if (frame < from || frame >= from + duration) continue;
      layers.push(`text:${i}`);
      const local = frame - from;
      const last = Math.max(0, Math.ceil(duration) - 1);
      const enter = Math.min(storySubtitleTheme.enterFrames - 1, last);
      const exit = Math.max(enter, last - (storySubtitleTheme.exitFrames - 1));
      const divider = Math.min(storySubtitleTheme.dividerEnterFrames - 1, last);
      if (local < Math.max(enter, divider) || local > exit) dynamic = true;
    }
    const key = dynamic ? `dynamic:${frame}` : layers.join("|");
    if (key !== previous) representatives.push(frame);
    indices.push(representatives.length - 1);
    previous = key;
  }
  return { representatives, indices };
}
