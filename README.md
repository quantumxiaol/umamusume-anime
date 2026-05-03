# Umamusume Anime by Agent

本项目把小说 draft 转成带角色配音、双语字幕和简单画面的 Remotion 视频。

核心思路：

```text
draft/*.md
  -> Agent 拆成结构化剧本
  -> TTS 生成逐句音频
  -> director.py 合成逐句图片和 timeline.json
  -> Remotion 渲染视频
```

Remotion 只负责播放图片、音频和字幕，不负责理解角色、场景、谁在说话。

---

## 现在做什么

当前流程已经能生成视频了，

当前结果（示例）：

```text
47 段剧情
27 段角色语音
47 张 3840x2160 PNG
27 个 MP3
约 311 秒 timeline
```

训练员和旁白，已经赛马娘等角色都可以使用 TTS 配音。

---

## Usage

启动一个Agent，启动TTS后端。
```
你有生成视频的skills .agents/skills/umamusume-video-pipeline/SKILL.md，我有一个draft/Bourbon_and_Trainer.md（美浦波旁的剧本）做视频，角色我已经准备好了，训练员、美浦波旁都有角色形象和音频，旁白也有音频。你需要先转成合适的剧本，主要靠角色对话来推动剧情，旁白尽量克制一点，少一点。然后走TTS合成音频，然后生成我的视频。
```

## 用什么做

### Agent

Codex / Antigravity / Claude 等 Agent 负责从 `draft/*.md` 生成结构化剧本：

- 拆分旁白、训练员、角色对白
- 生成日语台词和中文字幕
- 绑定 `speakerId`
- 选择背景别名
- 指定画面中出现的角色和站位
- 给需要配音的句子写入 `audio`

### TTS

当前实际使用 [Qwen3-TTS](https://github.com/quantumxiaol/Qwen3-TTS) 生成日语角色台词。

```text
server:  http://127.0.0.1:8001
script:  scripts/synthesize_script.py
input:   draft/*_script.json
output:  draft/*_audio/*.wav
```

IndexTTS 后端已准备好，后续可用于“日语克隆说中文”的路线。

### 导演层

`scripts/director.py` 负责把剧本、背景、立绘、音频变成 Remotion 素材：

- 读取结构化剧本
- 用 `scripts/background_catalog.json` 找背景图
- 用 `characters/<speaker_id>/` 找角色立绘
- 合成 `images/<line_id>.png`
- 把 WAV 转成 MP3
- 根据音频真实时长生成 `timeline.json`

角色出现、站位、背景选择都在这一层完成。

### Remotion

`my-video/` 是 Remotion 工程，只负责：

- 读取 `my-video/public/content/<project>/timeline.json`
- 播放 `images/*.png`
- 播放 `audio/*.mp3`
- 显示双语字幕
- 渲染 MP4

Remotion 不消费 `speakerId`、`characters`、`activeSpeaker`。

---

## 外部项目依赖

本仓库负责把素材和模型服务串成视频流水线，但部分输入来自其他本地项目。

### 角色构建

角色目录 `characters/<speaker_id>/` 依赖[umamusume-character-build](https://github.com/quantumxiaol/umamusume-character-build)

该项目负责准备角色相关素材，例如：

- 角色立绘 PNG
- `reference.mp3`
- `reference_jp.txt`
- `reference_zh.txt`
- 角色配置和提示词

本项目只读取这些结果，不负责角色素材构建。

### 背景爬取

背景目录 `backgrounds/` 依赖[umamusume-web-crawler](https://github.com/quantumxiaol/umamusume-web-crawler)，或者可以手动访问umamusu.wiki等下载准备图片。

该项目负责爬取或整理背景图。本项目通过 `scripts/background_catalog.json` 给这些背景建立英文别名，然后在剧本里引用别名。

### TTS 服务

日语角色配音依赖Qwen3-TTS，当前脚本默认调用本地 Qwen3-TTS 服务，中文配音或跨语种克隆路线依赖index-tts，当前使用 Qwen3-TTS 生成日语角色台词；IndexTTS 后续可用于“日语克隆说中文”的版本。

---

## 文件组织

```text
.
├── scripts/
│   ├── background_catalog.json
│   ├── director.py
│   └── synthesize_script.py
├── my_tts/
│   └── cli.py
├── my-video/
│   ├── src/
│   ├── public/content/
│   └── out/
├── draft/
├── characters/
└── backgrounds/
```

### 主要目录

`draft/`

- 小说草案
- 结构化剧本
- TTS 输出 WAV

示例：

```text
draft/endday.md
draft/endday_final_script.json
draft/endday_final_audio/*.wav
```

`characters/`

每个角色一个目录：

```text
characters/rice_shower/
  reference.mp3
  reference_jp.txt
  reference_zh.txt
  JSF_Rice-Shower.png
  ZF_Rice-Shower.png
  config.json
  prompt.md
```

当前 TTS 使用 `reference.mp3` 和 `reference_jp.txt`。  
当前导演层使用 `JSF_*.png` / `ZF_*.png`。

`backgrounds/`

- 原始背景图目录
- 文件名可以保留原始编号
- 通过 `scripts/background_catalog.json` 做英文别名映射

`scripts/background_catalog.json`

- 背景别名表
- 剧本里写 `classroom_day`、`track_day` 等别名
- 脚本根据别名找到实际 PNG

`my-video/public/content/<project>/`

Remotion 的输入目录：

```text
my-video/public/content/EndDay_Final_4k/
  descriptor.json
  timeline.json
  images/
  audio/
```

这是生成目录。

---

## 剧本 JSON

结构化剧本最重要的是 `lines`。

每一项通常包含：

```text
id          句子 ID，也是图片和音频文件名
type        dialogue 或 narration
speakerId   角色 ID，对应 characters/<speaker_id>/
background  背景别名，对应 background_catalog.json
characters  当前画面中出现的角色和站位
audio       需要配音的句子才写
spokenText  送入 TTS 的文本
subtitleJa  日语字幕
subtitleZh  中文字幕
showSpeaker false 时不显示说话者立绘，适合训练员
```

训练员和旁白可以没有 `audio`。没有音频时，导演层会用文本长度估算持续时间。

---

## 工作流程

### 1. Agent 拆剧本

输入：

```text
draft/endday.md
```

输出：

```text
draft/endday_final_script.json
```

这一步由 Codex / Antigravity / Claude 等 Agent 完成。

### 2. 启动 Qwen3-TTS

在 Qwen3-TTS 项目目录启动：

```bash
qwen-tts-server \
  --host 0.0.0.0 \
  --port 8001 \
  --base-model ./Qwen3-TTS-12Hz-1.7B-Base \
  --custom-model ./Qwen3-TTS-12Hz-1.7B-CustomVoice \
  --storage-root ./storage/qwen3_tts_service \
  --device mps \
  --dtype float16
```

### 3. 生成 TTS

生成全部缺失音频：

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --timeout 900 \
  --top-p 0.8 \
  --temperature 0.7
```

只重做某个角色：

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --speaker-id rice_shower \
  --overwrite \
  --timeout 900 \
  --top-p 0.8 \
  --temperature 0.7
```

输出：

```text
draft/endday_final_audio/*.wav
```

### 4. 合成图片和 timeline

4K：

```bash
uv run python scripts/director.py build \
  --script draft/endday_final_script.json \
  --project EndDay_Final_4k \
  --width 3840 \
  --height 2160 \
  --overwrite
```

1080p：

```bash
uv run python scripts/director.py build \
  --script draft/endday_final_script.json \
  --project EndDay_Final_1080p \
  --width 1920 \
  --height 1080 \
  --overwrite
```

输出：

```text
my-video/public/content/<project>/
```

### 5. Remotion 抽样验证

目录名可以叫 `EndDay_Final_4k`，但 Remotion composition id 不能包含 `_`。  
所以渲染 id 使用 `EndDay-Final-4k`。

```bash
cd my-video
pnpm exec remotion still EndDay-Final-4k \
  --output /tmp/EndDay_Final_4k-still.png \
  --frame=320 \
  --scale=0.25
```

### 6. 渲染视频

```bash
cd my-video
pnpm exec remotion render EndDay-Final-4k \
  out/EndDay_Final_4k.mp4 \
  --codec h264 \
  --crf 20
```

输出：

```text
my-video/out/EndDay_Final_4k.mp4
```

---

## Remotion timeline

`scripts/director.py` 生成的 `timeline.json` 包含：

```text
shortTitle  视频标题
width       视频宽度
height      视频高度
elements    图片序列和时间
text        字幕和时间
audio       音频和时间
```

Remotion 读取：

```text
images/<imageUrl>.png
audio/<audioUrl>.mp3
```

---

## 后续增强

当前 MVP 已经可以交付。后续优先增强两类能力：

### 场景规划和生图 prompt

如果结构化剧本需要某个场景，但 `scripts/background_catalog.json` 里没有合适背景：

- Agent 不要直接阻塞整个流程。
- 先在剧本或 sidecar 计划里写出场景描述和生图 prompt。
- 用户补充背景图后，再把它加入 `backgrounds/` 和 `background_catalog.json`。
- director 继续只根据背景别名找本地图片。

### 更细的演出控制

可逐步增加：

- 谁说话谁放大
- 非说话角色变暗
- 角色进出场
- 简单推拉镜头
- BGM / SFX
- 字幕样式主题

优先在导演层或 timeline 元数据中实现这些控制，不要让 Remotion 反向理解剧情语义。

---

## Agent Skill

本仓库提供项目 Skill：

```text
.agents/skills/umamusume-video-pipeline/SKILL.md
```

Agent 在处理本项目视频任务时应使用该 Skill。它固化了：

- 怎么写剧本 JSON
- 怎么检查 Qwen3-TTS 服务
- 怎么调用 TTS
- 怎么跑 `director.py`
- 怎么验证 Remotion still
- 怎么渲染最终 MP4
- 失败后从哪一步恢复

---

## 失败恢复

- TTS 中断：重启 TTS 服务，重新跑 `synthesize_script.py`，已有音频默认跳过。
- 角色参考音频更新：用 `--speaker-id <id> --overwrite` 只重做该角色。
- 背景、立绘、站位变了：重新跑 `director.py build --overwrite`。
- 字幕样式或 Remotion 代码变了：直接重新 `remotion still` 或 `remotion render`。
