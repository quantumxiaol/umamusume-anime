import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import assert from "node:assert/strict";
import { staticFramePlan } from "../src/lib/static-frame-plan";

async function main() {
  const req = createRequire(__filename);
  const r = createRequire(req.resolve("@remotion/cli"))("@remotion/renderer");
  const [bundle, id] = process.argv.slice(2);
  const browser = await r.openBrowser("chrome");
  try {
    const inputProps = { renderFps: 60 };
    const composition = await r.selectComposition({
      serveUrl: bundle,
      id,
      inputProps,
      puppeteerInstance: browser,
    });
    const plan = staticFramePlan(
      composition.props.timeline,
      60,
      60,
      0,
      composition.durationInFrames - 1,
    );
    const samples = new Set([0, 59, 60, 100, 200, 400, 500, 700, 839]);
    for (const f of plan.representatives) {
      samples.add(f);
      if (f > 0) samples.add(f - 1);
    }
    const hash = (b: Buffer) => createHash("sha256").update(b).digest("hex");
    for (const frame of [...samples].sort((a, b) => a - b)) {
      if (frame >= composition.durationInFrames) continue;
      const original = await r.renderStill({
        serveUrl: bundle,
        composition,
        inputProps,
        puppeteerInstance: browser,
        frame,
        imageFormat: "png",
      });
      const mappedProps = {
        ...composition.props,
        frameMap: plan.representatives,
      };
      const mapped = await r.renderStill({
        serveUrl: bundle,
        composition: { ...composition, props: mappedProps },
        inputProps: mappedProps,
        puppeteerInstance: browser,
        frame: plan.indices[frame],
        imageFormat: "png",
      });
      assert.equal(
        hash(original.buffer),
        hash(mapped.buffer),
        `Pixel mismatch at original frame ${frame}`,
      );
    }
    console.log(
      `Exact PNG comparison passed for ${samples.size} boundary/hold frames`,
    );
  } finally {
    await browser.close({ silent: true });
  }
}
main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
