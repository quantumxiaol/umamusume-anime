import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
const localRequire = createRequire(__filename);
const renderer = createRequire(localRequire.resolve("@remotion/cli"))(
  "@remotion/renderer",
);

async function main() {
  const [bundle, id, planPath] = process.argv.slice(2);
  const plan = JSON.parse(readFileSync(planPath, "utf8"));
  const browser = await renderer.openBrowser("chrome");
  try {
    const inputProps = { renderFps: 60 };
    const composition = await renderer.selectComposition({
      serveUrl: bundle,
      id,
      inputProps,
      puppeteerInstance: browser,
    });
    for (const still of plan) {
      console.log(`preview ${still.id} frame=${still.frame}`);
      await renderer.renderStill({
        serveUrl: bundle,
        composition,
        inputProps,
        puppeteerInstance: browser,
        frame: still.frame,
        output: still.output,
        scale: 0.25,
        imageFormat: "png",
      });
    }
  } finally {
    await browser.close({ silent: true });
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
