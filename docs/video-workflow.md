# 固定的定位、返修与生产入口

在仓库根目录运行 `.venv/bin/python scripts/video_workflow.py --help`。
这是给人和 Agent 共用的本地 CLI，不需要另写一份项目专用 Python 工具。
剧本仍用原来的 `lines` / `scenes[].lines` 协议；不改场景结构，不改 RoleTone 评分及替换规则。
配置、候选、报告、指纹都放 `draft/` 等已忽略目录，不需要提交 Git。

## 1. 从成片定位

```bash
.venv/bin/python scripts/video_workflow.py locate \
  --script draft/mcqueen_stay_with_you_script.json --at 01:23.5

.venv/bin/python scripts/video_workflow.py locate \
  --script draft/mcqueen_stay_with_you_script.json --text '明天'

.venv/bin/python scripts/video_workflow.py locate \
  --script draft/mcqueen_stay_with_you_script.json --speaker-id mejiro_mcqueen
```

时间是**成片播放器时间**，默认已包含 1 秒片头。句间空白归到前一句，并标记 `inGap`。
输出包括稳定 ID、台词、WAV 路径、起止时间、QC、生成记录和哈希是否仍匹配。
`qcMatchesAudio` 只说明报告匹配当前 WAV；参考音是否改变请用后面的 TTS 指纹审计。
多个筛选条件取交集；多个 `--line-id` / `--speaker-id` 各自取并集。

单条结果可加 `--video my-video/out/xxx.mp4 --clip /tmp/listen.wav`，导出前后各 1.5 秒的试听片段；不覆盖已有文件。
旧 MP4 必须配套它当时的时间轴，可用 `--timeline /path/to/timeline.json`；工具不能从视频反推出已丢失的句子 ID。
工作流 QC 可用 `--qc-report draft/xxx.workflow.work/audio_qc/qc_report.json` 指定。

插入新句子用 `new-id --prefix msw` 分配新 ID，不要重跑顺序编号把旧句子改号。工具检查重复 ID / 重复音频路径，但不会替你重写旧的项目构建器。

## 2. 只返修选中的句子或角色

```bash
.venv/bin/python scripts/video_workflow.py repair create \
  --script draft/mcqueen_stay_with_you_script.json \
  --line-id msw031 --work-dir draft/mcqueen_retry_01

.venv/bin/python scripts/video_workflow.py repair generate \
  --work-dir draft/mcqueen_retry_01 -- \
  --tts-engine fishspeech --fish-tts-url http://127.0.0.1:8002 \
  --model-id s2-pro-local-v1 --timeout 900 --batch-size 4 \
  --temperature 0.7 --top-p 0.8 --max-new-tokens 512

.venv/bin/python scripts/video_workflow.py repair compare --work-dir draft/mcqueen_retry_01
# 查看 comparison_report.json 并试听，再明确应用：
.venv/bin/python scripts/video_workflow.py repair apply --work-dir draft/mcqueen_retry_01
```

换整个人物的参考音后，把 `--line-id` 换成 `--speaker-id gold_ship` 即可。
`create` 只建立选中句子的候选工作区；`generate` 不接触原 WAV；`compare` 不替换。
`apply` 继续调用已有的、哈希绑定的 `audio_qc compare --apply`，备份被替换的原 WAV。
**不提供强制绕过 RoleTone 的自动替换**：原音频分高时，新候选可能被保留而不应用；以比较报告的实际决定为准。
原剧本、原音频或候选字节变了，会拒绝使用过期比较。再试一轮用新的工作目录。

需要调整台词/情绪时，给 `create` 加 `--patch draft/fixes.json`：

```json
{
  "msw031": {
    "spokenText": "[soft tone]今度は、私の言葉を聞いてください。",
    "subtitleJa": "今度は、私の言葉を聞いてください。",
    "subtitleZh": "这一次，请听听我的话。"
  }
}
```

只在候选真正应用后，才把对应编辑合回原剧本；没通过的候选不改原台词。不修改其他角色 WAV。
纯字幕修改直接改原 JSON，然后 Director → 字幕预检 → 预览 → 渲染，无需走音频返修。
应用后必须重做 QC / Director / 预览；声音时长变化会重排后续时间轴。

## 3. 音频指纹与旧音频迁移

现在每个新生成的 `a.wav` 都有 `a.wav.tts.json`，逐句落盘，记录：

- 实际提交文本（包含最终 Fish 标记）、角色；参考音 SHA-256 和参考文本。
- 后端、操作者指定的模型版本、生成参数、客户端代码指纹、请求模式。
- 输出 WAV SHA-256、生成时间、端点及参数记录。

模型版本通过 `--model-id` 声明，**不是自动读取或校验服务端权重**。换权重、服务端默认参数或实现时，须更新这个版本号；没有版本号的音频不宣称已完整验证。

用生成时相同参数审计（不联系 TTS，不改音频）：

```bash
.venv/bin/python scripts/synthesize_script.py \
  --script draft/mcqueen_stay_with_you_script.json \
  --tts-engine fishspeech --model-id s2-pro-local-v1 \
  --batch-size 4 --temperature 0.7 --top-p 0.8 --max-new-tokens 512 \
  --audit --audit-output /tmp/audio-cache.json
```

状态为 `missing`、`fresh`、`unverified`（旧音频/信息不完整）、`stale`（输入变了）、`modified`（WAV 被外部替换）、`accepted`（已审阅的返修候选）。
只改字幕不触发 TTS 过期。指纹不匹配会停下，不会静默复用，也不会自动整片重做。
旧项目明确决定继续保留原音频时，可加 `--reuse-unverified`；仍保留“未验证”状态，不伪造历史。此参数不能跳过已知过期或外部修改的音频。

返修可使用不同采样参数。已有生成记录时会记住原生产配置；旧项目建议在 `repair create` 时加 `--config draft/xxx.workflow.json`。
通过比较、明确应用后，工具同时保存“实际候选参数”和“本次审阅所接受的原生产请求”，避免下一次继续流程又把已接受的候选当作过期；之后再改文本、参考音或生产参数，仍然失效。

## 4. 字幕预检与按 ID 预览

先运行 Director 更新时间轴，再执行：

```bash
.venv/bin/python scripts/video_workflow.py subtitles \
  --script draft/mcqueen_stay_with_you_script.json --output /tmp/subtitles.json

.venv/bin/python scripts/video_workflow.py preview \
  --script draft/mcqueen_stay_with_you_script.json \
  --line-id msw001 --line-id msw036 --output /tmp/mcqueen-stills
```

预检在 Chromium 中测量与成片**共用的字幕组件和字体**，报告实际换行、越界、末行过短、行数过多、遮挡上部画面、中文字幕混入假名和疑似 TTS 标记。
这些是复核提示，不自动改译文或断行。文化称呼、翻译正确性、人物遮挡是否合理仍由人/Agent 看图判断。
预览自动计算 60fps + 1 秒片头后的帧号，最小资源打包一次，串行生成 1/4 尺寸 PNG。不启动完整视频渲染。

## 5. 一套参数贯穿流程，两个明确检查点

```bash
.venv/bin/python scripts/video_workflow.py init \
  --script draft/mcqueen_stay_with_you_script.json \
  --output draft/mcqueen.workflow.json \
  --tts-engine fishspeech --tts-url http://127.0.0.1:8002 --model-id s2-pro-local-v1

.venv/bin/python scripts/video_workflow.py run --config draft/mcqueen.workflow.json
# TTS / QC 后暂停；读报告并试听，必要时先返修：
.venv/bin/python scripts/video_workflow.py approve --config draft/mcqueen.workflow.json --stage audio
.venv/bin/python scripts/video_workflow.py run --config draft/mcqueen.workflow.json
# Director / 字幕预检 / 预览后暂停；检阅图片：
.venv/bin/python scripts/video_workflow.py approve --config draft/mcqueen.workflow.json --stage visual
.venv/bin/python scripts/video_workflow.py run --config draft/mcqueen.workflow.json --until render
```

`init` 默认采用 batch=4、temperature=0.7、top-p=0.8、max-new-tokens=512，可编辑配置中的 `tts.args`。
旧音频要继续沿用，将 `--reuse-unverified` 加入这个数组；默认不替用户作此决定。
短音频/字幕警告需要查看后再 `approve ... --accept-warnings`，不改变 RoleTone 阈值；音频错误或评分失败不能批准。
预览默认覆盖首/中/末句、背景/人物布置发生变化的句子，以及所有字幕告警。
检查点保存在配置旁的 `.work/`，输入和报告有哈希绑定；变更后自动要求重新检查。不是素材管理系统，也不要求 Git 保存产物。
渲染只委托 `scripts/render_4k60.sh`，保持 **3840×2160、60fps、0.1 秒句间隔**，保留其内存、磁盘、TTS 关闭和分块续渲染检查。这个工具不会自行重启 TTS。

这些命令负责机械执行和安全复用；从小说写出角色口吻、译文、服装及连贯场景，仍由 Agent 创作，不交给固定模板自动压缩。

## 6. 可选事件 CG：独立计划，不改剧本协议

CG 由带角色/场景参考的生图工具准备并人工审阅，不由 Director 自动生成。
在独立 JSON 中用稳定句子 ID 绑定闭区间，区间结束后自动恢复原立绘：

```json
{
  "schemaVersion": 1,
  "projectId": "Story_4k60",
  "shots": [{
    "id": "confession",
    "startLineId": "s062",
    "endLineId": "s066",
    "sceneId": "park_bench",
    "background": "park_promenade_day",
    "image": "draft/cg/confession.png"
  }]
}
```

`projectId` 指原剧本项目；图片路径与背景路径一样，以仓库根目录为基准，或用绝对路径。
每个覆盖句必须具有相同的 `sceneId` 和解析到相同文件的 `background`；嵌套剧本可从场景 `sceneId` / `id` 继承。
未知/重复句子 ID、倒序区间、重叠 CG、跨场景或跨背景会拒绝构建。程序只能验证场景声明，插画实际是否画对地点、服装、肢体仍要目视核对。

```bash
.venv/bin/python scripts/video_workflow.py init \
  --script draft/story_script.json --output draft/story_cg.workflow.json \
  --project Story_CG_4k60 --cg-plan draft/story_cg_plan.json \
  --tts-engine fishspeech --tts-url http://127.0.0.1:8002 --model-id s2-pro-local-v1
```

新配置的 `project` 可单独指定输出，保留原版视频；`cgPlan` 为可选项。继续沿用音频审阅 → 预览审阅 → 渲染流程。
CG 不改变任何配音/字幕/时间戳，覆盖时不再叠加或缩放旧人物立绘。CG 图片与计划参与视觉审阅指纹；修改它们要重新预览，但不触发 TTS。
正式预览会额外覆盖每个 CG 的前一句、首句、末句和后一句，检查切入/切回及字幕遮挡。

只做画面工作的低层入口仍可用：

```bash
.venv/bin/python scripts/director.py build --script draft/story_script.json \
  --project Story_CG_4k60 --cg-plan draft/story_cg_plan.json \
  --width 3840 --height 2160 --line-gap-ms 100
```

删除 `cgPlan` / 不传 `--cg-plan` 即走原有立绘流程。既有脚本、音频指纹、Remotion 时间轴协议都不变。
