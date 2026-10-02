import type { ReactElement } from "react";
import { createRequire } from "node:module";
import { readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { SubtitleLayout } from "../src/components/Subtitle";
import { TimelineSchema } from "../src/lib/types";

const localRequire = createRequire(__filename);
const { renderToStaticMarkup } = localRequire("react-dom/server") as {
  renderToStaticMarkup: (element: ReactElement) => string;
};
const renderer = createRequire(localRequire.resolve("@remotion/cli"))(
  "@remotion/renderer",
);

async function main() {
  const [source, output] = process.argv.slice(2);
  if (!source || !output)
    throw new Error("Expected timeline.json and output report.json");
  const bytes = readFileSync(source);
  const timeline = TimelineSchema.parse(JSON.parse(bytes.toString()));
  const width = timeline.width ?? 1920,
    height = timeline.height ?? 1080;
  const browser = await renderer.openBrowser("chrome");
  try {
    const page = await browser.newPage({
      context: null,
      logLevel: "error",
      indent: false,
      pageIndex: 0,
      onBrowserLog: null,
      onLog: () => {},
    });
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    const entries = [];
    for (const item of timeline.text) {
      const html = renderToStaticMarkup(
        <SubtitleLayout
          item={item}
          width={width}
          height={height}
          frame={30}
          durationInFrames={100}
        />,
      );
      const measured = await page.evaluate(async (markup: string) => {
        document.body.innerHTML = markup;
        document.body.style.cssText = "margin:0;padding:0;";
        const style = document.createElement("style");
        style.textContent =
          "*,::before,::after{box-sizing:border-box;border:0 solid;margin:0;padding:0}";
        document.body.appendChild(style);
        await document.fonts.ready;
        const box = document
          .querySelector('[data-subtitle-role="box"]')
          ?.getBoundingClientRect();
        const languages = [
          ...document.querySelectorAll<HTMLElement>("[data-subtitle-role]"),
        ]
          .filter((el) => el.dataset.subtitleRole !== "box")
          .map((el) => {
            const rect = el.getBoundingClientRect();
            const node = el.firstChild;
            const rows: {
              y: number;
              text: string;
              left: number;
              right: number;
            }[] = [];
            if (node?.nodeType === Node.TEXT_NODE) {
              let offset = 0;
              for (const char of node.textContent ?? "") {
                const range = document.createRange();
                range.setStart(node, offset);
                offset += char.length;
                range.setEnd(node, offset);
                const r = range.getBoundingClientRect();
                if (!char.trim() || !r.height) continue;
                let row = rows.find((x) => Math.abs(x.y - r.y) < 2);
                if (!row) {
                  row = { y: r.y, text: "", left: r.left, right: r.right };
                  rows.push(row);
                }
                row.text += char;
                row.left = Math.min(row.left, r.left);
                row.right = Math.max(row.right, r.right);
              }
            }
            return {
              language: el.dataset.subtitleRole,
              rows,
              font: getComputedStyle(el).font,
              overflow:
                rows.some(
                  (row) =>
                    row.left < rect.left - 1 ||
                    row.right > rect.right + 1 ||
                    row.left < 0 ||
                    row.right > innerWidth,
                ) ||
                rect.top < 0 ||
                rect.bottom > innerHeight,
            };
          });
        return { top: box?.top ?? innerHeight, languages };
      }, html);
      const issues: string[] = [];
      for (const lang of measured.languages) {
        if (lang.overflow) issues.push(`${lang.language}:overflow`);
        if (
          lang.rows.length > 1 &&
          [...lang.rows[lang.rows.length - 1].text].length <= 3
        )
          issues.push(`${lang.language}:short-last-row`);
        if (lang.rows.length > (lang.language === "speaker" ? 1 : 3))
          issues.push(`${lang.language}:many-rows`);
      }
      if (measured.top < height * 0.55)
        issues.push("subtitle-covers-upper-picture");
      if (/[\u3040-\u30ff]/u.test(item.subtitleZh ?? ""))
        issues.push("zh:contains-kana");
      if (
        /\[.*?\]|<\|.*?\|>/u.test(
          (item.subtitleJa ?? "") + (item.subtitleZh ?? ""),
        )
      )
        issues.push("possible-tts-tag-in-subtitle");
      entries.push({ id: item.id, ...measured, issues });
    }
    writeFileSync(
      output,
      JSON.stringify(
        {
          schemaVersion: 1,
          timelineSha256: createHash("sha256").update(bytes).digest("hex"),
          width,
          height,
          lines: entries,
          flagged: entries.filter((x) => x.issues.length).map((x) => x.id),
        },
        null,
        2,
      ),
    );
    console.log(
      `Measured ${entries.length} subtitles; ${entries.filter((x) => x.issues.length).length} need review: ${output}`,
    );
  } finally {
    await browser.close({ silent: true });
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
