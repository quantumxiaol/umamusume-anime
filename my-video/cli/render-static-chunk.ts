import { createRequire } from "node:module";
import {
  mkdtempSync,
  mkdirSync,
  writeFileSync,
  linkSync,
  rmSync,
} from "node:fs";
import { join, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { staticFramePlan } from "../src/lib/static-frame-plan";

// Resolve the renderer shipped with our pinned CLI, not a globally installed one.
const localRequire = createRequire(__filename);
const renderer = createRequire(localRequire.resolve("@remotion/cli"))(
  "@remotion/renderer",
);
async function main() {
  const [
    bundle,
    id,
    output,
    startArg,
    endArg,
    fpsArg,
    crf,
    preset,
    concurrencyArg = "1",
  ] = process.argv.slice(2);
  const start = Number(startArg),
    end = Number(endArg),
    fps = Number(fpsArg);
  const concurrency = Number(concurrencyArg);
  if (
    !output ||
    !Number.isInteger(start) ||
    !Number.isInteger(end) ||
    end < start ||
    !Number.isInteger(concurrency) ||
    concurrency < 1
  ) {
    throw new Error(
      "Expected bundle, composition, output, start, end, fps, crf, preset",
    );
  }
  const temp = mkdtempSync(join(dirname(output), ".static-frames-"));
  const started = performance.now();
  let browser;
  try {
    browser = await renderer.openBrowser("chrome");
    const inputProps = { renderFps: fps };
    const composition = await renderer.selectComposition({
      serveUrl: bundle,
      id,
      inputProps,
      puppeteerInstance: browser,
    });
    const plan = staticFramePlan(
      composition.props.timeline,
      fps,
      fps,
      start,
      end,
    );
    const compactProps = {
      ...composition.props,
      frameMap: plan.representatives,
    };
    console.log(
      `static plan: ${plan.representatives.length}/${plan.indices.length} browser frames`,
    );
    const images = join(temp, "images");
    mkdirSync(images);
    await renderer.renderFrames({
      serveUrl: bundle,
      composition: { ...composition, props: compactProps },
      frameRange: [0, plan.representatives.length - 1],
      inputProps: compactProps,
      puppeteerInstance: browser,
      concurrency,
      muted: true,
      imageFormat: "jpeg",
      jpegQuality: 80,
      outputDir: null,
      onFrameBuffer: (buffer: Buffer, frame: number) =>
        writeFileSync(join(images, `${frame}.jpg`), buffer),
      onStart: () => {},
      onFrameUpdate: (done: number) => {
        if (done % 50 === 0)
          console.log(`browser frames: ${done}/${plan.representatives.length}`);
      },
    });
    const framesDone = performance.now();
    // Reuse identical JPEG bytes via hard links: no second screenshot or pixel copy.
    const sequence = join(temp, "sequence");
    mkdirSync(sequence);
    for (const [frame, index] of plan.indices.entries()) {
      linkSync(
        join(images, `${index}.jpg`),
        join(sequence, `${String(frame).padStart(8, "0")}.jpg`),
      );
    }
    const audio = join(temp, "audio.wav");
    await renderer.renderMedia({
      serveUrl: bundle,
      composition,
      inputProps,
      puppeteerInstance: browser,
      codec: "wav",
      outputLocation: audio,
      frameRange: [start, end],
      sampleRate: 48000,
      enforceAudioTrack: true,
      concurrency,
      overwrite: true,
    });
    await browser.close({ silent: true });
    browser = undefined;
    const audioDone = performance.now();
    const samples = (plan.indices.length * 48000) / fps;
    const ffmpeg = renderer.RenderInternals.getExecutablePath({
      type: "ffmpeg",
      indent: false,
      logLevel: "error",
      binariesDirectory: null,
    });
    const result = spawnSync(
      ffmpeg,
      [
        "-y",
        "-v",
        "warning",
        "-framerate",
        String(fps),
        "-i",
        join(sequence, "%08d.jpg"),
        "-i",
        audio,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-frames:v",
        String(plan.indices.length),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        crf,
        "-pix_fmt",
        "yuvj420p",
        "-af",
        `aresample=48000:async=0:first_pts=0,apad=whole_len=${samples},atrim=end_sample=${samples},asetpts=N/SR/TB`,
        "-c:a",
        "aac",
        "-b:a",
        "320k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        output,
      ],
      {
        stdio: "inherit",
        cwd: dirname(ffmpeg),
        env: {
          ...process.env,
          DYLD_LIBRARY_PATH: dirname(ffmpeg),
          LD_LIBRARY_PATH: dirname(ffmpeg),
        },
      },
    );
    if (result.error || result.status !== 0)
      throw result.error ?? new Error(`ffmpeg exited ${result.status}`);
    console.log(
      JSON.stringify({
        browserFrames: plan.representatives.length,
        outputFrames: plan.indices.length,
        browserSeconds: (framesDone - started) / 1000,
        audioSeconds: (audioDone - framesDone) / 1000,
        encodeSeconds: (performance.now() - audioDone) / 1000,
      }),
    );
  } finally {
    if (browser) await browser.close({ silent: true });
    rmSync(temp, { recursive: true, force: true });
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
