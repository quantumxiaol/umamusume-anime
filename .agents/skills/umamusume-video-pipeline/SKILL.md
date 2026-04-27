---
name: umamusume-video-pipeline
description: Use for this repository's local Uma Musume draft-to-video pipeline: turning draft/*.md into structured script JSON, generating per-line Qwen3-TTS audio, building director-composited images and Remotion timeline content, validating stills, and rendering MP4. Trigger when asked to create or update a story video, generate TTS, run director.py, build 1080p/4K content, validate Remotion output, or recover a failed pipeline step.
---

# Umamusume Video Pipeline

## Overview

Use this skill to run the repo's current production flow:

```text
draft/*.md -> draft/*_script.json -> draft/*_audio/*.wav -> my-video/public/content/<project>/ -> Remotion MP4
```

Keep Remotion as a renderer. Do not move character semantics, speaker selection, background selection, or staging logic into Remotion unless the user explicitly asks to change architecture.

## Current Architecture

- Agent writes `draft/*_script.json` from `draft/*.md`.
- `scripts/synthesize_script.py` calls Qwen3-TTS and writes `draft/*_audio/*.wav`.
- `scripts/director.py` composites backgrounds and character sprites into `images/<line_id>.png`, converts audio to MP3, and writes `timeline.json`.
- Remotion reads `my-video/public/content/<project>/timeline.json`, plays PNG/MP3/subtitles, and renders video.

Generated/local assets are ignored by git: `draft/`, `characters/`, `backgrounds/`, `my-video/public/content/`, `my-video/out/`.

## Script JSON Contract

Write a top-level object with `projectId`, `title`, and `lines`.

Each line should use these fields:

- `id`: stable line id; also used as image/audio basename.
- `type`: `dialogue` or `narration`.
- `speakerId`: character id matching `characters/<speaker_id>/`; use `trainer` for trainer.
- `background`: alias in `scripts/background_catalog.json`.
- `characters`: optional list of on-screen sprites; use `slot` values `left`, `right`, `center_left`, `center_right`, `center`.
- `characters[].spriteScale`: optional base scale only for special framing. Do not use it to mark the speaking character.
- `audio`: only for voiced character lines, usually `draft/<project>_audio/<line_id>.wav`.
- `spokenText`: text sent to TTS.
- `subtitleJa` and `subtitleZh`: two-line bilingual subtitles.
- `showSpeaker: false`: for trainer or other non-visual speakers.

Trainer and narration can be voiced if they have `characters/<speaker_id>/reference.mp3` or `reference.wav`.

The director automatically applies active-speaker styling per line:

- `line.speakerId == characters[].speakerId`: active character, slightly larger and full brightness.
- Other visible characters: slightly smaller and dimmed.
- Narration/voice-over lines: neutral framing; visible characters are not emphasized.

Keep JSON focused on who is present and where they stand. Avoid hand-writing per-line active-speaker scale changes in the script.

For EndDay conventions:

- Agnes Tachyon calls trainer `トレーナー君` or `モルモット君`.
- Mihono Bourbon uses `Master`.
- Rice Shower uses `お兄さま`.

## Workflow

### 1. Inspect Inputs

Before generating content, check:

```bash
rg --files draft characters backgrounds scripts my-video/src | sort
```

Confirm required character assets exist:

```text
characters/<speaker_id>/reference.mp3
characters/<speaker_id>/reference_jp.txt
characters/<speaker_id>/JSF_*.png or ZF_*.png
```

List known backgrounds when needed:

```bash
uv run python scripts/director.py list-backgrounds
```

### 2. Create or Update Script JSON

Use Agent judgment to adapt the draft into 35-50 short lines for a final video unless the user requests a different length.

Validate JSON and basic fields:

```bash
uv run python - <<'PY'
import json
from pathlib import Path
p = Path("draft/endday_final_script.json")
d = json.load(p.open(encoding="utf-8"))
lines = d.get("lines", [])
required = ["id", "type", "background", "spokenText", "subtitleJa", "subtitleZh"]
missing = [(i, line.get("id"), key) for i, line in enumerate(lines, 1) for key in required if not line.get(key)]
audio_missing_speaker = [(i, line.get("id")) for i, line in enumerate(lines, 1) if line.get("audio") and not line.get("speakerId")]
print("lines", len(lines))
print("voiced", sum(1 for line in lines if line.get("audio")))
print("missing", missing)
print("audio_missing_speaker", audio_missing_speaker)
print("ids_unique", len({line.get("id") for line in lines}) == len(lines))
PY
```

### 3. Check Qwen3-TTS Service

Use the local TTS service at `http://127.0.0.1:8001`.

```bash
uv run python - <<'PY'
from my_tts.cli import Qwen3TTSClient
c = Qwen3TTSClient("http://127.0.0.1:8001", timeout=10)
try:
    print(c.health())
finally:
    c.close()
PY
```

If health times out, tell the user to restart `qwen-tts-server` before sending generation requests.

### 4. Generate TTS

Generate missing audio:

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --timeout 900 \
  --top-p 0.8 \
  --temperature 0.7
```

Regenerate one character after reference audio changes:

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --speaker-id rice_shower \
  --overwrite \
  --timeout 900 \
  --top-p 0.8 \
  --temperature 0.7
```

Verify every `audio` path exists before running director:

```bash
uv run python - <<'PY'
import json
from pathlib import Path
d = json.load(open("draft/endday_final_script.json", encoding="utf-8"))
missing = [(line["id"], line["audio"]) for line in d["lines"] if line.get("audio") and not Path(line["audio"]).exists()]
print("missing", missing)
PY
```

### 5. Build Director Content

Build 4K:

```bash
uv run python scripts/director.py build \
  --script draft/endday_final_script.json \
  --project EndDay_Final_4k \
  --width 3840 \
  --height 2160 \
  --overwrite
```

Build 1080p:

```bash
uv run python scripts/director.py build \
  --script draft/endday_final_script.json \
  --project EndDay_Final_1080p \
  --width 1920 \
  --height 1080 \
  --overwrite
```

Verify counts and dimensions:

```bash
file my-video/public/content/EndDay_Final_4k/images/t001.png
uv run python - <<'PY'
import json
d = json.load(open("my-video/public/content/EndDay_Final_4k/timeline.json", encoding="utf-8"))
print(d["width"], d["height"], len(d["elements"]), len(d["audio"]), len(d["text"]))
print(round(d["elements"][-1]["endMs"] / 1000, 2))
PY
```

### 6. Validate and Render Remotion

Composition IDs cannot contain `_`. A content directory named `EndDay_Final_4k` is rendered with composition id `EndDay-Final-4k`.

Still validation:

```bash
cd my-video
pnpm exec remotion still EndDay-Final-4k \
  --output /tmp/EndDay_Final_4k-still.png \
  --frame=320 \
  --scale=0.25
```

Final render:

```bash
cd my-video
pnpm exec remotion render EndDay-Final-4k \
  out/EndDay_Final_4k.mp4 \
  --codec h264 \
  --crf 20
```

Current FPS is defined in `my-video/src/lib/constants.ts` as `FPS = 30`.

## Recovery Rules

- TTS timeout: restart Qwen3-TTS, rerun synthesis; existing audio is skipped unless `--overwrite` is used.
- Character reference changed: rerun synthesis with `--speaker-id <id> --overwrite`, then rerun director.
- Background, sprite, placement, subtitle text changed: rerun director; no TTS needed unless `spokenText` changed.
- Remotion subtitle/style code changed: rerun still/render; no TTS or director needed unless baked images must change.

## Future Extensions

- Scene prompt fallback: if a requested background alias is missing, add a scene prompt field or TODO entry rather than blocking the whole script. Keep generated prompts in the script or a sidecar plan until the user adds assets.
- Richer staging: speaker zoom, inactive-character dimming, enter/exit, pan/zoom, BGM/SFX, and subtitle themes should initially be implemented in the director layer or as timeline styling metadata. Do not make Remotion infer story semantics.
