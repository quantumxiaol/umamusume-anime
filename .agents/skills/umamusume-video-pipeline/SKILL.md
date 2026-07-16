---
name: umamusume-video-pipeline
description: "Use for this repository's local Uma Musume draft-to-video pipeline: turning draft/*.md into structured script JSON, generating per-line Qwen3-TTS or Fish Speech audio, building director-composited images and Remotion timeline content, applying the macOS pre-render memory gate, validating stills, and rendering MP4. Trigger when asked to create or update a story video, generate TTS, run director.py, build 1080p/4K content, validate Remotion output, or recover a failed pipeline step."
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
- `scripts/synthesize_script.py` calls Qwen3-TTS or Fish Speech and writes `draft/*_audio/*.wav`.
- `scripts/audio_qc.py` validates WAVs, optionally scores them with one shared RoleTone model, and writes `qc_report.json` plus `retry_plan.json`.
- `scripts/director.py` incrementally composites content-addressed `images/frame-<sha256>.png`, converts changed audio to MP3, and writes `timeline.json` plus `.director-manifest.json`.
- Remotion reads `my-video/public/content/<project>/timeline.json`, plays PNG/MP3/subtitles, and renders video.
- `scripts/render_4k60.sh` owns final 4K60 memory/TTS/disk gates, one-time minimal bundle, chunk rendering, resume, assembly, and media verification.

Generated/local assets are ignored by git: `draft/`, `characters/`, `backgrounds/`, `my-video/public/content/`, `my-video/out/`.

## Script JSON Contract

Write a top-level object with `projectId`, `title`, and `lines`.

Each line should use these fields:

- `id`: stable logical line id. Audio commonly keeps it as a basename; Director images are content-addressed and may be shared by many lines.
- `type`: `dialogue` or `narration`.
- `speakerId`: character id matching `characters/<speaker_id>/`; use `trainer` for trainer.
- `background`: alias in `scripts/background_catalog.json`.
- `characters`: optional list of on-screen sprites; use `slot` values `left`, `right`, `center_left`, `center_right`, `center`.
- `characters[].spriteScale`: optional base scale only for special framing. Do not use it to mark the speaking character.
- `audio`: only for voiced character lines, usually `draft/<project>_audio/<line_id>.wav`.
- `spokenText`: text sent to TTS. For Fish Speech, put inline emotion/style tags here when a line needs per-line control, for example `[excited][pitch up]ブーケちゃん、そこは負けないんだ`.
- `subtitleJa` and `subtitleZh`: two-line bilingual subtitles.
- `showSpeaker: false`: for trainer or other non-visual speakers.

For Fish Speech scripts, prefer inline tags at the start of `spokenText` and keep subtitles clean:

```json
{
  "spokenText": "[soft tone]午後の庭園で、トレーナーは久しぶりに肩の力を抜いていた",
  "subtitleJa": "午後の庭園で、トレーナーは久しぶりに肩の力を抜いていた",
  "subtitleZh": "午后的庭园里，训练员久违地放松了紧绷的肩膀"
}
```

Do not put `<|speaker:0|>` into normal script JSON; the Fish Speech client adds it before sending requests. Existing inline Fish style tags in `spokenText` are preserved, and global `--fish-style` is only inserted when no leading style tag exists.

Fish Speech tags are not a closed enum, especially with S2-Pro natural-language bracket tags, but a small stable set is more predictable for anime dialogue:

| Use case | Recommended S2-Pro tag | Notes |
| --- | --- | --- |
| bright greeting / normal cheerful line | `[bright and cheerful tone]` | Good default for upbeat character dialogue. |
| cute delighted line | `[delighted][pitch up]` | Good for Curren-like cute greetings, playful discoveries, and light affection. |
| energetic but not shouting | `[excited]` | First choice before trying volume changes. |
| stronger energetic line | `[excited][volume up]` | Use sparingly; can become too forceful. |
| confident declaration | `[confident]` | Good for rivalry, promises, and proud remarks. |
| teasing / playful | `[playful tone]` | Good for light teasing; avoid on serious lines. |
| gentle / caring | `[soft tone]` | Good for tea, comfort, reflection, and narration. |
| quick / hurried | `[in a hurry tone]` | Good for flustered or fast-paced lines. |
| small surprise | `[surprised][pitch up]` | Use on short reactions; can be unstable on long lines. |

Use at most 1-2 tags for most lines. Prefer tone/pitch tags over aggressive tags such as `[shouting]` or `[screaming]`, unless the story really needs it. If a tag makes the voice drift or sound overacted, remove the second tag first, then lower sampling to about `--temperature 0.85 --top-p 0.9`.

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

### 3. Check TTS Service

Do not hard-code or infer the TTS backend from its port. Select it explicitly for each pipeline run and keep the same `TTS_ENGINE`/`TTS_URL` through synthesis and the pre-render shutdown gate. The user's current preferred backend is Fish Speech, so use this for the normal path unless the user explicitly requests Qwen3-TTS or only Qwen3-TTS is available:

```bash
export TTS_ENGINE=fishspeech
export TTS_URL=http://127.0.0.1:8002
uv run my-tts fish health --base-url "$TTS_URL" --timeout 10
```

Fish Speech normally runs on `http://127.0.0.1:8002`, but it may use `8001`; keep `TTS_ENGINE=fishspeech` and set `TTS_URL` to the actual loopback URL.

For an explicitly selected Qwen3-TTS run:

```bash
export TTS_ENGINE=qwen3tts
export TTS_URL=http://127.0.0.1:8001
uv run my-tts qwen health --base-url "$TTS_URL" --timeout 10
```

If health times out, tell the user to restart the active TTS server before sending generation requests.

### 4. Generate TTS

Always pass `--tts-engine` explicitly and pass the matching URL from `TTS_URL`; `scripts/synthesize_script.py` requires an explicit backend so an omitted flag cannot silently select the wrong service. For the current normal workflow, select Fish Speech with `--tts-engine fishspeech`. It supports single-line `voice_clone` and multi-line `voice_clone_batch_file`. For Fish Speech S2 Pro, keep batches shorter because memory use is higher; use `--batch-size 2` to `--batch-size 4` for safer runs.

Fish Speech command (current preferred path):

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --tts-engine fishspeech \
  --fish-tts-url "$TTS_URL" \
  --timeout 900 \
  --batch-size 4 \
  --temperature 0.7 \
  --top-p 0.8 \
  --max-new-tokens 512
```

The Qwen3-TTS path remains available when it is explicitly selected. Qwen3-TTS uses `voice_clone_batch_file` grouped by `speakerId`.

Qwen3-TTS command:

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --tts-engine qwen3tts \
  --qwen3tts-url "$TTS_URL" \
  --timeout 900 \
  --batch-size 6 \
  --non-streaming-mode true \
  --do-sample true \
  --temperature 0.6 \
  --top-p 0.85 \
  --top-k 20 \
  --subtalker-do-sample true \
  --subtalker-temperature 0.6 \
  --subtalker-top-p 0.85 \
  --subtalker-top-k 20
```

Fish Speech target text is normalized before the request:

- `<|speaker:0|>` is automatically added unless `--no-fish-speaker-tag` is passed.
- Preferred per-line control is to put Fish Speech tags directly in `spokenText`, for example `[excited][pitch up]...` or `[soft tone]...`.
- `--fish-style <preset>` inserts a batch-default inline emotion/style tag only when `spokenText` does not already start with a style tag. For S2-Pro, the default syntax is `s2`, so `--fish-style energetic` becomes `[excited][volume up]`. For S1-mini, pass `--fish-style-syntax s1` to use tags like `(excited)`.
- Use `--fish-style-tag '[excited][pitch up]'` to pass an exact inline tag.
- Per-line script fields also work as a structured alternative: `fishStyle`, `fishEmotion`, `fishStyleTag`, and `fishStyleSyntax`.

Good first-pass Fish Speech S2-Pro settings for livelier anime dialogue:

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --tts-engine fishspeech \
  --fish-tts-url "$TTS_URL" \
  --timeout 900 \
  --batch-size 4 \
  --fish-style energetic \
  --fish-style-syntax s2 \
  --temperature 0.9 \
  --top-p 0.9 \
  --max-new-tokens 512
```

If Fish Speech is running on `8001`, update the recorded URL before health checks and synthesis:

```bash
export TTS_URL=http://127.0.0.1:8001
```

Regenerate one character:

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --tts-engine fishspeech \
  --fish-tts-url "$TTS_URL" \
  --speaker-id kitasan_black \
  --overwrite \
  --timeout 900 \
  --batch-size 4 \
  --temperature 0.7 \
  --top-p 0.8 \
  --max-new-tokens 512
```

Regenerate one line:

```bash
uv run python scripts/synthesize_script.py \
  --script draft/endday_final_script.json \
  --tts-engine fishspeech \
  --fish-tts-url "$TTS_URL" \
  --line-id kb001 \
  --overwrite \
  --timeout 900 \
  --no-batch \
  --temperature 0.7 \
  --top-p 0.8 \
  --max-new-tokens 256
```

Backend-specific notes:

- Qwen3-TTS supports `--language`, `--non-streaming-mode`, `--do-sample`, `--top-k`, and `--subtalker-*`.
- Fish Speech supports `--temperature`, `--top-p`, `--max-new-tokens`, `--chunk-length`, `--seed`, `--use-memory-cache`, `--fish-format`, and Fish-side text style flags like `--fish-style`.
- Do not pass Qwen-only tuning flags expecting Fish Speech to use them.
- Fish Speech S2 Pro can be slower and memory-heavy; prefer short lines, smaller batches, and explicit `--max-new-tokens`.
- If a short line sounds unstable, use the same repair approach as Qwen: regenerate, rephrase, or generate with a context lead-in and cut the target sentence.

Verify every `audio` path exists before running director:

```bash
uv run python - <<'PYCODE'
import json
from pathlib import Path
d = json.load(open("draft/endday_final_script.json", encoding="utf-8"))
missing = [(line["id"], line["audio"]) for line in d["lines"] if line.get("audio") and not Path(line["audio"]).exists()]
print("missing", missing)
PYCODE
```

### 5. Score TTS With RoleTone

Run the tracked QC stage after synthesis and before any TTS shutdown. RoleTone is explicit and
offline by default; the tool groups lines by speaker, resolves each local reference, and loads one
WavLM model for the whole run:

```bash
uv sync --extra all
```

This one-time optional-extra install is required before using `--roletone`. Objective WAV checks
work without it.

```bash
uv run python scripts/audio_qc.py check \
  --script draft/endday_final_script.json \
  --roletone
```

Read both generated files before continuing:

- `draft/endday_final_audio_qc/qc_report.json`
- `draft/endday_final_audio_qc/retry_plan.json`

Treat low scores as a review queue, not an automatic failure. WavLM can false-negative on very short lines, noisy starts, breathy attacks, or lines with unusual emotion. For visibly low and audibly bad lines, try in this order:

- Regenerate with the same text and low-temperature sampling.
- Shorten or rephrase the spoken text while keeping subtitles unchanged if needed.
- Use context generation plus cut: generate a longer line that leads into the target sentence, then cut out only the target audio.

Put retry WAVs under a candidate directory and compare without mutation first:

```bash
uv run python scripts/audio_qc.py compare \
  --script draft/endday_final_script.json \
  --candidates-dir draft/endday_final_audio_retry \
  --roletone
```

Only after reviewing `comparison_report.json`, repeat the same command with `--apply`. The apply
phase verifies the reviewed script, settings, current-WAV hash, and candidate-WAV hash; stale bytes
are refused. It keeps content-addressed backups of every replaced version and replaces only when the
candidate strictly passes RoleTone and objective quality does not regress. If QC or repair changes
audio, rerun Director before rendering.

### 6. Build Director Content

Build 4K:

```bash
uv run python scripts/director.py build \
  --script draft/endday_final_script.json \
  --project EndDay_Final_4k60 \
  --width 3840 \
  --height 2160
```

Build 1080p:

```bash
uv run python scripts/director.py build \
  --script draft/endday_final_script.json \
  --project EndDay_Final_1080p \
  --width 1920 \
  --height 1080
```

Director owns `.director-manifest.json`. It reuses `frame-<sha256>.png` for identical visual
inputs, skips unchanged images/audio, rewrites timing after changed audio, and prunes unreferenced
generated PNG/MP3 files only after a successful build. Use `--overwrite` only to force a rebuild;
within-run visual deduplication still applies.

Verify dimensions, logical line count, and the smaller set of unique image references:

```bash
uv run python - <<'PY'
import json
d = json.load(open("my-video/public/content/EndDay_Final_4k60/timeline.json", encoding="utf-8"))
print(d["width"], d["height"], len(d["elements"]), len(d["audio"]), len(d["text"]))
print("unique_images", len({item["imageUrl"] for item in d["elements"]}))
print(round(d["elements"][-1]["endMs"] / 1000, 2))
PY
```

### 7. Pre-render Memory Safety (macOS)

Do not shut down TTS while synthesis, QC, or retry work remains. Final 4K60 rendering must go
through `scripts/render_4k60.sh`; it implements the authoritative gate:

- Require the exact backend and loopback URL used by this run; never infer a backend from a port.
- Health-check only that backend, request graceful shutdown, require `server_stopped: true`, and
  confirm its TCP listener is gone. Only connection-refused means it was already stopped.
- Measure `memory_pressure -Q` before and after shutdown. Missing/invalid output blocks rendering.
- Require at least 40% post-shutdown free pressure for one worker. Each extra active Remotion
  worker adds 10 percentage points of required headroom.
- Require at least 20 GiB of free scratch per outer chunk job on both the temp and output volumes.
- Never use `pkill` or process-name matching as a fallback.

If shutdown returns an error, times out, lacks exit confirmation, or the listener remains, stop.
Restart an old TTS server with code that supports the admin shutdown endpoint before retrying.

### 8. Validate and Render Remotion

Composition IDs cannot contain `_`. A content directory named `EndDay_Final_4k60` is rendered with composition id `EndDay-Final-4k60`.

Still validation:

```bash
cd my-video
pnpm exec remotion still EndDay-Final-4k60 \
  --output /tmp/EndDay_Final_4k60-still.png \
  --frame=320 \
  --scale=0.25 \
  --concurrency=1
```

Render stills one at a time. Never launch multiple 4K Remotion still/render commands through parallel tool calls or subagents.

Final 4K60 render:

```bash
scripts/render_4k60.sh \
  --project EndDay_Final_4k60 \
  --tts-engine fishspeech \
  --tts-url http://127.0.0.1:8002
```

The renderer defaults to `--jobs 1 --render-concurrency 1`. When the user explicitly wants
parallel chunks, use `--jobs 2` first. It may use up to four total workers only when the increasing
memory and scratch gates pass. Keep inner concurrency at one unless there is a measured reason to
trade outer jobs for inner workers.

The renderer acquires one repository-wide lock, creates a minimal public root containing only the
target project, bundles Remotion exactly once, reuses that bundle for every chunk, uses bounded
`--disallow-parallel-encoding`, and cleans the temporary bundle. It resumes only chunks whose media
specification and SHA-256 render signature match, then rebuilds audio with a documented sub-
millisecond codec-timestamp tolerance, fully decodes, and validates the final MP4. Do not use old
standalone render/assembly implementations; any local project wrapper must contain parameters only
and delegate to `scripts/render_4k60.sh`.

`my-video/src/lib/constants.ts` keeps a 30fps fallback for ordinary previews. The unified renderer
passes `renderFps: 60`; `Root.tsx` resolves the final composition metadata to 60fps. Do not change
the fallback constant merely to produce a 4K60 final.

## Recovery Rules

- TTS timeout: restart the selected backend, rerun synthesis; existing audio is skipped unless `--overwrite` is used.
- TTS shutdown is only a phase transition after audio QC. If later repair is required, restart the selected backend, regenerate only flagged lines, rerun `audio_qc.py`, then rerun Director and the unified renderer.
- Qwen3-TTS/Fish Speech shutdown `404`: the running server predates its admin endpoint. Do not start a memory-heavy render; have the user restart the updated server or close it manually.
- Shutdown accepted but exit confirmation times out: do not render and do not use a broad process kill. Report which backend did not stop cleanly.
- Character reference changed: rerun synthesis with `--speaker-id <id> --overwrite`, rerun QC, then rerun Director.
- Background, sprite, placement, subtitle text changed: rerun Director without `--overwrite`; its manifest rebuilds only affected outputs. No TTS is needed unless `spokenText` changed.
- Remotion subtitle/style code changed: rerun still/render; no TTS or director needed unless baked images must change.
- Interrupted 4K60 render: rerun `scripts/render_4k60.sh` with the same media parameters. Scheduling such as `--jobs` may change; only matching media/signature chunks are reused.

## Future Extensions

- Scene prompt fallback: if a requested background alias is missing, add a scene prompt field or TODO entry rather than blocking the whole script. Keep generated prompts in the script or a sidecar plan until the user adds assets.
- Richer staging: speaker zoom, inactive-character dimming, enter/exit, pan/zoom, BGM/SFX, and subtitle themes should initially be implemented in the director layer or as timeline styling metadata. Do not make Remotion infer story semantics.
