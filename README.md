# Umamusume Anime by Agent

本项目把小说 draft 转成带角色配音、双语字幕和简单画面的 Remotion 视频。

核心思路：

```text
draft/*.md
  -> Agent 拆成结构化剧本
  -> TTS 生成逐句音频
  -> audio_qc.py 做客观音频检查和 RoleTone 评分
  -> director.py 增量合成去重画面和 timeline.json
  -> render_4k60.sh 分块、校验并渲染视频
```

Remotion 只负责播放图片、音频和字幕，不负责理解角色、场景、谁在说话。

---

## 现在做什么

当前流程已经能生成视频了，

当前结果（示例）：

```text
47 段剧情
27 段角色语音
47 段画面引用（相同布景只保存一张 3840x2160 PNG）
27 个 MP3
约 311 秒 timeline
```

训练员和旁白，已经赛马娘等角色都可以使用 TTS 配音。

---

## Usage

启动一个Agent，启动TTS后端。
```
你有生成视频的skills .agents/skills/umamusume-video-pipeline/SKILL.md。
我现在有一个draft/Bourbon_and_Trainer.md（美浦波旁的剧本）做视频，角色我已经准备好了，有对应的角色形象和音频，旁白也有音频。
你需要先转成合适的剧本，主要靠角色对话来推动剧情，旁白尽量克制一点，少一点。然后走TTS合成音频。可以使用Fish Speech TTS。
生成出的音频可以用RoleTone打分看看。分数太低的可以尝试重新生成，换台词，或者是使用引导句再cut。
对话句子如果太长，可以拆成两句。注意如果一句中有句号，可能使TTS停顿较长时间。
每句之间应该是有0.1s的间隔。
然后生成我的视频。
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

日语角色台词支持 Fish Speech 和 Qwen3-TTS，当前工作流优先使用 Fish Speech。

```text
server:  Fish Speech 常用 http://127.0.0.1:8002；Qwen3-TTS 常用 http://127.0.0.1:8001
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
- 用视觉输入的 SHA-256 复用相同的 `images/frame-<sha256>.png`
- 把 WAV 转成 MP3
- 根据音频真实时长生成 `timeline.json`
- 用 `.director-manifest.json` 跳过未变化的画面和音频

角色出现、站位、背景选择都在这一层完成。

### Remotion

`my-video/` 是 Remotion 工程，只负责：

- 读取 `my-video/public/content/<project>/timeline.json`
- 播放 `images/*.png`
- 播放 `audio/*.mp3`
- 按 timeline 中已经确定的 `speakerLabel`、`subtitleJa`、`subtitleZh` 排版双语字幕
- 渲染 MP4

Remotion 不读取剧本里的 `characters`、`activeSpeaker`，也不会根据 `speakerId` 猜人物名或翻译字幕；这些语义由 Agent 和 director 层确定。timeline 可以保留 `speakerId` 作为字幕元数据。

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

日语角色配音支持 Fish Speech 和 Qwen3-TTS，当前工作流优先使用 Fish Speech。`scripts/synthesize_script.py` 要求显式传入 `--tts-engine fishspeech` 或 `--tts-engine qwen3tts`，不会根据端口或缺省值猜测后端。中文配音或跨语种克隆路线依赖 IndexTTS，后续可用于“日语克隆说中文”的版本。

---

## 文件组织

```text
.
├── scripts/
│   ├── background_catalog.json
│   ├── audio_qc.py
│   ├── director.py
│   ├── render_4k60.py
│   ├── render_4k60.sh
│   └── synthesize_script.py
├── my_tts/
│   └── cli.py
├── my-video/
│   ├── src/
│   ├── public/content/
│   └── out/
├── third_party/
│   ├── RoleTone/
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
speakerLabel 可选的字幕显示名；未写时 director 会尝试从角色 config 或内置的训练员/旁白名称解析
background  背景别名，对应 background_catalog.json
characters  当前画面中出现的角色和站位
audio       需要配音的句子才写
spokenText  送入 TTS 的文本；Fish Speech 可在开头写 [excited]、[soft tone] 等情绪标记
subtitleJa  日语字幕
subtitleZh  中文字幕
showSpeaker false 时不显示说话者立绘，适合训练员
```

Fish Speech 的情绪标记只放在 `spokenText`，字幕字段保持干净。例如：

```json
{
  "spokenText": "[soft tone]午後の庭園で、トレーナーは久しぶりに肩の力を抜いていた",
  "subtitleJa": "午後の庭園で、トレーナーは久しぶりに肩の力を抜いていた",
  "subtitleZh": "午后的庭园里，训练员久违地放松了紧绷的肩膀"
}
```

不用在剧本里写 `<|speaker:0|>`；Fish Speech client 会在请求前自动补。

Fish Speech S2-Pro 的方括号 tag 不是严格枚举，模型可以理解不少自然语言描述；但实际批量生产时，先用一小组稳定 tag 会更可控：

| 场景 | 推荐 tag | 备注 |
| --- | --- | --- |
| 明亮问候 / 普通活泼 | `[bright and cheerful tone]` | 适合作为开场和日常对白默认值 |
| 可爱开心 / 轻微撒娇 | `[delighted][pitch up]` | 适合真机伶这类可爱发现、亲昵称呼 |
| 有精神但不喊 | `[excited]` | 比加音量更稳 |
| 更强的元气感 | `[excited][volume up]` | 少量使用，容易过强 |
| 自信宣言 | `[confident]` | 适合胜负、承诺、得意发言 |
| 打趣 / 调皮 | `[playful tone]` | 适合轻微捉弄，不适合严肃句 |
| 温柔 / 关心 / 旁白 | `[soft tone]` | 适合茶会、安慰、缓慢叙述 |
| 慌张 / 节奏快 | `[in a hurry tone]` | 适合短句，不宜长段 |
| 轻微惊喜 | `[surprised][pitch up]` | 适合短反应，长句可能不稳 |

多数句子用 1-2 个 tag 就够了。优先使用 tone / pitch 类，不要一上来用 `[shouting]`、`[screaming]` 这类强标签；如果声音飘或表演过头，先删第二个 tag，再把采样降到大约 `--temperature 0.85 --top-p 0.9`。

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

### 2. 启动 TTS 后端

Qwen3-TTS 默认端口是 `8001`。

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

Fish Speech 默认端口建议用 `8002`，这样不会和 Qwen3-TTS 冲突。如果本机同一时间只跑一个 TTS 后端，也可以继续用 `8001`，调用脚本时传 `--fish-tts-url http://127.0.0.1:8001`。

在 Fish Speech 项目目录启动：

```bash
fish-tts-server \
  --host 0.0.0.0 \
  --port 8002 \
  --llama-checkpoint-path ./modelsweights/s2-pro \
  --decoder-checkpoint-path ./modelsweights/s2-pro/codec.pth \
  --storage-root ./storage/fish_speech_service \
  --device mps \
  --dtype float16 \
  --max-seq-len 4096
```

### 3. 生成 TTS

显式选择 Qwen3-TTS 生成全部缺失音频：

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --tts-engine qwen3tts \
  --qwen3tts-url http://127.0.0.1:8001 \
  --timeout 900 \
  --top-p 0.8 \
  --temperature 0.7
```

使用 Fish Speech 生成全部缺失音频：

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --tts-engine fishspeech \
  --fish-tts-url http://127.0.0.1:8002 \
  --timeout 900 \
  --batch-size 4 \
  --temperature 0.7 \
  --top-p 0.8
```

Fish Speech 目标文本会默认自动补 `<|speaker:0|>`。推荐在 script JSON 的 `spokenText` 开头直接写情绪标记，方便批量生成时逐句控制：

```json
{
  "id": "cc001",
  "speakerId": "curren_chan",
  "spokenText": "[excited][pitch up]ブーケちゃん、そこは負けないんだ",
  "subtitleJa": "ブーケちゃん、そこは負けないんだ",
  "subtitleZh": "小花，这一点可不能输哦"
}
```

如果整批都想先套一个默认风格，也可以用命令行风格：

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --tts-engine fishspeech \
  --fish-tts-url http://127.0.0.1:8002 \
  --batch-size 4 \
  --fish-style energetic \
  --fish-style-syntax s2 \
  --temperature 0.9 \
  --top-p 0.9
```

常用 `--fish-style` 取值：`bright`、`cute`、`confident`、`energetic`、`excited`、`fast`、`joyful`、`satisfied`、`soft`、`teasing`。S2-Pro 默认用方括号风格标记，例如 `energetic` 会转成 `[excited][volume up]`；如果 `spokenText` 已经以 `[tag]` 或 `(tag)` 开头，client 不会再重复插入命令行风格。`fishStyle`、`fishEmotion`、`fishStyleTag` 这些逐句字段仍然可用，但优先推荐直接写在 `spokenText` 开头。

只重做某个角色：

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --tts-engine fishspeech \
  --fish-tts-url http://127.0.0.1:8002 \
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

### 4. 音频 QC 和 RoleTone

先做基础解码、采样率、声道、时长、峰值、RMS、静音和削波检查；传入
`--roletone` 后会按人物自动选择 `characters/<speaker>/reference.mp3` 或
`reference.wav`，并在同一进程复用一个 WavLM 模型：

首次使用 RoleTone 时安装可选依赖；只做客观 WAV 检查则不需要：

```bash
uv sync --extra all
```

```bash
uv run python scripts/audio_qc.py check \
  --script draft/endday_final_script.json \
  --roletone
```

默认输出到 `draft/endday_final_audio_qc/qc_report.json` 和
`retry_plan.json`。低分是重试队列，不会直接改写原 WAV。重生成候选音频后先比较：

```bash
uv run python scripts/audio_qc.py compare \
  --script draft/endday_final_script.json \
  --candidates-dir draft/endday_final_audio_retry \
  --roletone
```

确认报告后再用完全相同的参数加 `--apply`。应用阶段会核对剧本、设置、原音频和
候选音频的 SHA-256；任何文件在审核后发生变化都会拒绝替换。工具只会替换
RoleTone 严格通过且客观音质不下降的候选，并为每一版被替换的原 WAV 建立内容寻址备份。

### 5. 合成图片和 timeline

4K：

```bash
uv run python scripts/director.py build \
  --script draft/endday_final_script.json \
  --project EndDay_Final_4k60 \
  --width 3840 \
  --height 2160
```

1080p：

```bash
uv run python scripts/director.py build \
  --script draft/endday_final_script.json \
  --project EndDay_Final_1080p \
  --width 1920 \
  --height 1080
```

Director 会读取 `.director-manifest.json`：字幕变化只改 timeline，音频变化只处理对应
MP3，背景、立绘、站位、焦点或画布变化才重绘受影响的 PNG。相同视觉输入共享一张
PNG；`--overwrite` 只用于显式强制重建，同一次运行仍会去重。旧项目第一次迁移时，
只清理旧 timeline 明确引用的逐句图片/音频；不在旧 timeline 或 manifest 中的手工素材
不会被猜测为 Director 产物。

输出：

```text
my-video/public/content/<project>/
```

### 6. Remotion 抽样验证

目录名可以叫 `EndDay_Final_4k60`，但 Remotion composition id 不能包含 `_`。
所以渲染 id 使用 `EndDay-Final-4k60`。

```bash
cd my-video
pnpm exec remotion still EndDay-Final-4k60 \
  --output /tmp/EndDay_Final_4k60-still.png \
  --frame=320 \
  --scale=0.25
```

### 7. 渲染 4K60 视频

```bash
scripts/render_4k60.sh \
  --project EndDay_Final_4k60 \
  --tts-engine fishspeech \
  --tts-url http://127.0.0.1:8002
```

统一渲染器会获取仓库级锁、优雅关闭本次使用的 TTS、检查 listener、内存和磁盘，
从仅含当前项目的临时 public 目录 bundle 一次，再以默认串行、显式可控并行的方式渲染并校验所有 chunk。
有效 chunk 只有在媒体规格和渲染签名都匹配时才会续用。

源码中的 `DEFAULT_FPS = 30` 只作为普通预览回退值；统一渲染器传入
`renderFps: 60`，最终 composition metadata 和成片均按 60fps 校验。

TTS 关闭后需要并行两个 chunk 时显式加 `--jobs 2`。并行不会降低单个 Remotion
实例的默认 `--render-concurrency 1`，而且会自动提高内存和临时磁盘门槛；门槛不够时
直接拒绝启动。

输出：

```text
my-video/out/endday-final-4k60.mp4
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

`imageUrl` 是内容寻址的静态合成画面，不是视频逐帧编号。多句台词可以共享同一个
`imageUrl`；字幕、音频和时间仍按各自的 timeline 项独立渲染。

### 结构化字幕契约

director 新生成的 `text` 数组不再把“人物名 + 日文 + 中文”拼进一个 `text` 字符串，而是逐项写成结构化字段：

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

- `kind` 是 `dialogue` 或 `narration`。
- `speakerLabel` 优先使用剧本中的显式值；否则 director 尝试读取 `characters/<speaker_id>/config.json` 的 `name_zh`，训练员和旁白使用默认显示名。普通对白仍无法解析时会给出警告并省略人物名。
- `subtitleJa`、`subtitleZh` 分别保存日文和中文；如果旧剧本只提供 `subtitle` 或 `spokenText`，director 会把它作为日文侧的兜底文本，不自动杜撰翻译。
- `position` 目前由 director 写为 `bottom`。字幕层固定采用底部版式，不让旧 timeline 的 `position: "center"` 把字幕突然移回画面中央。

1080p 基准下，日文和中文都使用 `44px × uiScale` 的字号，并保持相同字重和行高；4K 等分辨率会随画面等比缩放。字幕自然换行，不再根据句子长度用 `fitText()` 偷偷缩小某一种语言。

### 旧 timeline 兼容

Remotion 仍可读取旧字幕项中的 `text`、`position` 和 `animations`。兼容层会尽量从旧的多行 `text` 中拆出人物名、日文和中文；无法可靠拆分时仍会显示原文。`position: "center"` 和旧的随机缩放 `animations` 只作为兼容元数据保留，新的字幕主题拥有自己的底部布局和入退场效果。

旧内容可以直接预览或重渲染。首次用新版 Director 构建旧项目时，会生成 manifest、
迁移到内容寻址 PNG，并在成功写出新 timeline 后清理当前项目 `images/` 和 `audio/`
中不再引用的生成文件。

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
- 怎么检查当前选择的 Fish Speech 或 Qwen3-TTS 服务
- 怎么调用 TTS
- 怎么跑 `director.py`
- 怎么验证 Remotion still
- 怎么渲染最终 MP4
- 失败后从哪一步恢复

---

## 失败恢复

- TTS 中断：重启 TTS 服务，重新跑 `synthesize_script.py`，已有音频默认跳过。
- 角色参考音频更新：用 `--speaker-id <id> --overwrite` 只重做该角色。
- 音频需要复查：重新运行 `audio_qc.py check --roletone`；候选音频先用 `compare`，确认后才 `--apply`。
- 背景、立绘、站位变了：直接重新跑 `director.py build`，manifest 只重绘受影响画面。
- `subtitleJa`、`subtitleZh`、`speakerLabel` 等字幕内容变了：直接重新跑 `director.py build`，不会重绘 PNG 或重转音频。
- 只修改 `my-video/src/` 中的字幕字号、颜色、间距、描边或动效：不需要重跑 director，直接使用现有 timeline 执行 `remotion still` 或 `remotion render`。
- 4K60 中断：用相同参数重新运行 `scripts/render_4k60.sh`；只有签名与媒体检查都通过的 chunk 才会续用。
