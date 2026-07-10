# Umamusume Anime Remotion Renderer

这个目录是仓库的视频渲染层。它读取 `public/content/<project>/timeline.json`，播放 director 已经准备好的图片、音频和字幕，并生成最终视频。

这里修改的是本项目的 React / Remotion 组件，不需要修改 Remotion 框架源码、`node_modules` 或 `@remotion/*` 包。

## 本地预览与渲染

```bash
pnpm install
pnpm run dev
```

项目目录可以包含下划线，例如 `EndDay_Final_4k`；composition id 会把下划线转换成连字符：

```bash
pnpm exec remotion still EndDay-Final-4k \
  --output /tmp/EndDay_Final_4k-still.png \
  --frame=320 \
  --scale=0.25

pnpm exec remotion render EndDay-Final-4k \
  out/EndDay_Final_4k.mp4 \
  --codec h264 \
  --crf 20
```

## Timeline 输入

当前生产入口是仓库根目录的 `scripts/director.py`，不是 `my-video/cli/`：

```text
scripts/director.py
  -> public/content/<project>/timeline.json
  -> src/ 中的 Remotion 组件
  -> MP4
```

timeline 顶层包含：

```text
shortTitle  视频标题
width       视频宽度
height      视频高度
elements    图片序列和时间
text        字幕序列和时间
audio       音频序列和时间
```

director 新生成的字幕项使用结构化字段：

```json
{
  "id": "tt002",
  "startMs": 14047,
  "endMs": 18970,
  "kind": "dialogue",
  "speakerId": "tokai_teio",
  "speakerLabel": "东海帝王",
  "subtitleJa": "いつもの甘えんぼ作戦じゃだめ。",
  "subtitleZh": "不能再用平时撒娇那套了。",
  "position": "bottom"
}
```

`speakerLabel`、`subtitleJa`、`subtitleZh` 是最终显示内容。Remotion 不根据 `speakerId` 猜人物名，也不负责翻译。

## 字幕样式

- 日文和中文使用相同的基准字号：1080p 下均为 `44px × uiScale`。
- 两种语言保持相同字重和行高，只通过垂直间距区分。
- 文本自然换行，不再使用 `fitText()` 根据长度缩小字号。
- 字幕采用底部版式；旧数据中的 `position: "center"` 不会把字幕移到画面中央。

## 旧 Timeline 兼容

旧生成文件可以继续使用。字幕兼容层仍接受：

```json
{
  "startMs": 14047,
  "endMs": 18970,
  "text": "东海帝王：日文台词\n中文字幕",
  "position": "center",
  "animations": []
}
```

兼容层会尽量从旧 `text` 中拆出人物名、日文和中文；无法可靠拆分时仍显示原文。旧 `position` 和字幕 `animations` 继续通过类型校验，但新的字幕层使用自己的底部布局和入退场主题。

`cli/timeline.ts` 来自旧模板，会继续生成扁平 `text`、`position: "center"` 和随机缩放 `animations`。它不是当前 Uma Musume 流水线的生产入口；若仍运行 `pnpm run gen`，输出会走上述兼容路径，视觉效果以当前字幕主题为准。

## 哪些修改需要重跑 director

- 只改 `src/` 中字幕的字号、颜色、间距、描边或动效：不需要重跑 director，直接重新 still / render。
- 修改剧本里的 `subtitleJa`、`subtitleZh`、`speakerLabel`，或希望把旧 timeline 迁移成结构化格式：重新运行 `scripts/director.py build --overwrite`，不需要重跑 TTS。
- 修改 `spokenText` 或配音文件：先按需重跑 TTS，再重跑 director。

本目录最初基于 Remotion AI Video template；当前仓库已将内容生成入口替换为本地 director 流水线。
