# my_tts

项目内的 Python CLI，分成两层能力：

- 项目级工作流：围绕 `my-video/public/content/<slug>` 批量替换音频并重建时间轴
- 服务级原子能力：直接调用本地 `Qwen3TTS` 服务的单条/批量接口

## 当前功能

### 1. 项目级

- `list-projects`
  - 扫描 `my-video/public/content` 下可用项目
- `clone-project`
  - 读取 `descriptor.json`
  - 读取项目当前 `audio/<uid>.mp3` 作为参考音频，或使用 `--shared-reference-audio`
  - 调用 `IndexTTS` 或 `Qwen3TTS` 生成新音频
  - 自动把结果转成 `mp3` 覆盖项目音频
  - 更新 `descriptor.json` 中的 `text`、`audioTimestamps`、`ttsMeta`
  - 重新生成 `timeline.json`
  - 默认把旧参考音频备份到 `reference-audio/`

`clone-project --engine qwen3tts` 现在会在满足下面条件时自动走新的 `voice_clone_batch_file` 接口：

- 提供了 `--shared-reference-audio`
- 且 `--qwen-x-vector-only-mode` 已开启，或者所有分段共用同一份参考文本

不满足时会自动回退到逐条 `voice_clone`。

### 2. Qwen3TTS 服务级

- `qwen health`
- `qwen list-narrators`
- `qwen voice-clone`
- `qwen voice-clone-batch-file`
- `qwen narration`
- `qwen narration-batch-file`

这些命令支持：

- 直接传文本或文本文件
- 可选把服务端返回的音频下载到本地
- 透传常见生成参数，如 `top_p`、`temperature`、`max_new_tokens`

## 依赖

- Python 环境：当前仓库的 `uv` 虚拟环境
- 可执行文件：`ffmpeg`、`ffprobe`
- 本地服务：
  - `IndexTTS` 默认 `http://127.0.0.1:8000`
  - `Qwen3TTS` 默认 `http://127.0.0.1:8001`

## 常用命令

查看项目：

```bash
uv run my-tts list-projects
```

项目级：用 `IndexTTS` 把一个项目改成中文音频：

```bash
uv run my-tts clone-project history-of-venus \
  --engine indextts \
  --script-file /path/to/chinese-lines.txt
```

项目级：用 `Qwen3TTS` 把一个项目改成日语音频：

```bash
uv run my-tts clone-project history-of-venus \
  --engine qwen3tts \
  --script-file /path/to/japanese-lines.txt \
  --reference-script-file /path/to/reference-lines.txt
```

项目级：多个分段共用一条参考音频时：

```bash
uv run my-tts clone-project history-of-venus \
  --engine qwen3tts \
  --shared-reference-audio /path/to/ref.wav \
  --script-file /path/to/japanese-lines.txt \
  --reference-script-file /path/to/ref-text.txt
```

服务级：直接测 Qwen 健康检查：

```bash
uv run my-tts qwen health
```

服务级：单条日语 voice clone：

```bash
uv run my-tts qwen voice-clone \
  --ref-audio /path/to/ref.wav \
  --text "もうすぐに新たな年が来る。" \
  --ref-text "もうすぐに新たな年が来る。" \
  --language Japanese \
  --download-to /tmp/qwen-single.wav
```

服务级：批量 voice clone：

```bash
uv run my-tts qwen voice-clone-batch-file \
  --ref-audio /path/to/ref.wav \
  --text-file /path/to/lines.txt \
  --ref-text "参考音频对应的文本" \
  --language Japanese \
  --download-dir /tmp/qwen-batch
```

服务级：批量 narration：

```bash
uv run my-tts qwen narration-batch-file \
  --text-file /path/to/lines.txt \
  --language Chinese \
  --speaker Uncle_Fu \
  --download-dir /tmp/qwen-narration
```

## 文本文件格式

项目级 `clone-project --script-file` 支持：

- `.txt`：每个非空行对应一个内容段，顺序和 `descriptor.json` 一致
- `.json`：字符串数组
- `.json`：`{ "uid": "text" }` 映射
