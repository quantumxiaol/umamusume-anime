---
name: umamusume-video-pipeline
description: Use for this repository's local Uma Musume draft-to-video pipeline: turning draft/*.md into structured script JSON, generating per-line Qwen3-TTS or Fish Speech audio, building director-composited images and Remotion timeline content, applying the macOS pre-render memory gate, validating stills, and rendering MP4. Trigger when asked to create or update a story video, generate TTS, run director.py, build 1080p/4K content, validate Remotion output, or recover a failed pipeline step.
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

RoleTone is vendored in this repo at `third_party/RoleTone` and installed by `uv sync --extra all`.
Use the project-local CLI, not the old external `/Volumes/.../RoleTone` project.

Check the CLI and available devices:

```bash
.venv/bin/roletone --help
.venv/bin/roletone devices
```

The repo-level `.env.example` contains the expected RoleTone defaults. The important variables are:

```text
HF_HOME=./modelsweights/huggingface
HF_HUB_DISABLE_XET=1
NUMBA_CACHE_DIR=/private/tmp/roletone_numba_cache
ROLETONE_MODEL=wavlm-base-plus-sv
ROLETONE_DEVICE=auto
```

Use CPU for comparable scoring unless there is a clear reason to benchmark another device.
On this Mac environment `roletone devices` may report `mps` unavailable even when TTS itself uses MPS.

Score a per-speaker candidate directory:

```bash
NUMBA_CACHE_DIR=/private/tmp/roletone_numba_cache \
.venv/bin/roletone score-dir \
  --reference characters/kitasan_black/reference.mp3 \
  --candidates-dir draft/kitasan_black_audio_candidates/roletone_revision/kitasan_black \
  --pattern "*.wav" \
  --output draft/kitasan_black_audio_candidates/kitasan_black_roletone_scores.csv \
  --model sv \
  --device cpu \
  --hf-home ./modelsweights/huggingface \
  --offline
```

If a script's audio directory mixes multiple speakers, create a temporary per-speaker directory with symlinks or copies before scoring. The reference must match the candidate speaker, for example `characters/<speaker_id>/reference.mp3`.

Treat low scores as a review queue, not an automatic failure. WavLM can false-negative on very short lines, noisy starts, breathy attacks, or lines with unusual emotion. For visibly low and audibly bad lines, try in this order:

- Regenerate with the same text and low-temperature sampling.
- Shorten or rephrase the spoken text while keeping subtitles unchanged if needed.
- Use context generation plus cut: generate a longer line that leads into the target sentence, then cut out only the target audio.

### 6. Build Director Content

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

### 7. Pre-render Memory Safety (macOS)

Run this gate after all TTS generation, RoleTone scoring, and audio repair are complete, but before any Remotion still or render command. Do not shut down the active TTS backend while more synthesis or retry work is pending.

Record the backend used by this pipeline run and its loopback URL. Only inspect or stop a service that this run actually used and that is dedicated to this local pipeline; do not probe or stop an unrelated TTS service. If both backends were deliberately used, repeat the backend-specific checks for both. Fish Speech may also run on port `8001`, so a port number alone does not identify the backend.

```bash
: "${TTS_ENGINE:?set TTS_ENGINE to qwen3tts or fishspeech to match synthesis}"
: "${TTS_URL:?set TTS_URL to the loopback URL used for synthesis}"
LC_ALL=C memory_pressure -Q
FREE_PERCENT="$(LC_ALL=C memory_pressure -Q | awk '/System-wide memory free percentage:/ {gsub(/%/, "", $NF); print $NF}')"
echo "memory_pressure_free_percent=$FREE_PERCENT"
case "$FREE_PERCENT" in
  ''|*[!0-9]*) echo "invalid memory_pressure free percentage" >&2; exit 1 ;;
esac
if [ "$FREE_PERCENT" -lt 0 ] || [ "$FREE_PERCENT" -gt 100 ]; then
  echo "memory_pressure free percentage out of range: $FREE_PERCENT" >&2
  exit 1
fi
```

Treat the `memory_pressure -Q` value as a free-percentage pressure indicator: higher is safer. Do not convert it into physical free GiB, and do not use swap-file count as the render gate. A missing, non-numeric, or out-of-range value blocks rendering.

Query only the selected backend. Use the matching branch, not both commands unconditionally:

```bash
case "$TTS_ENGINE" in
  qwen3tts) uv run my-tts qwen health --base-url "$TTS_URL" --timeout 5 ;;
  fishspeech) uv run my-tts fish health --base-url "$TTS_URL" --timeout 5 ;;
  *) echo "unsupported TTS_ENGINE: $TTS_ENGINE" >&2; exit 1 ;;
esac
```

Apply these rules:

- Qwen3-TTS is resident when `loaded_models` is non-empty; Fish Speech is resident when `loaded: true`.
- Before any final render (1080p, 4K, or 4K60) or a full-resolution 4K still, gracefully stop the selected resident TTS service once no more TTS work is needed, even when memory pressure currently looks acceptable. Loaded local TTS models can retain substantial MPS/CPU memory.
- A lightweight scaled 1080p still may keep the selected service running when `FREE_PERCENT >= 50`; otherwise stop it first if it is resident.
- Only an explicit connection-refused/`ConnectError` result means the backend is already stopped. A timeout, HTTP error, schema mismatch, wrong service, or invalid JSON leaves the state unknown: verify `TTS_ENGINE` and `TTS_URL`, then block rendering until resolved.
- Empty `loaded_models` or `loaded: false` means that backend has no model resident, but the post-check threshold below still applies.

Request a graceful shutdown for the selected backend and wait for Uvicorn to exit:

```bash
case "$TTS_ENGINE" in
  qwen3tts) uv run my-tts qwen shutdown --base-url "$TTS_URL" --timeout 5 --wait-timeout 60 ;;
  fishspeech) uv run my-tts fish shutdown --base-url "$TTS_URL" --timeout 5 --wait-timeout 60 ;;
esac
```

The result must contain `server_stopped: true`; a normal first request also has `status: accepted`, while an already-pending request may report `status: already_pending`. The server lets active TTS requests finish before exiting and rejects new requests after shutdown begins.

For the strict render gate, also confirm that the selected service's TCP listener is gone; a non-200 health response alone is not sufficient proof that Uvicorn exited. Derive the port from `TTS_URL` and use this guard:

```bash
TTS_PORT="$(uv run python -c 'import sys; from urllib.parse import urlsplit; u=urlsplit(sys.argv[1]); print(u.port or (443 if u.scheme == "https" else 80))' "$TTS_URL")"
if lsof -nP -iTCP:"$TTS_PORT" -sTCP:LISTEN; then
  echo "TTS listener is still active on port $TTS_PORT" >&2
  exit 1
fi
```

If `lsof` still reports a listener, stop before rendering. Shutdown accepts only an explicit loopback URL; if synthesis used `0.0.0.0` as the server bind address, use `127.0.0.1` or `localhost` with the same port for the client command.

Run `memory_pressure -Q` again after shutdown:

- `FREE_PERCENT < 40`: do not start a 4K/4K60 Remotion render. Report the low-memory condition and wait for the user or for other processes to release memory.
- `40 <= FREE_PERCENT < 50`: only use the serial, bounded-chunk low-memory render settings below.
- `FREE_PERCENT >= 50`: rendering may start, but 4K/4K60 still defaults to one Remotion process.

Also check temporary/output disk space before choosing non-parallel encoding:

```bash
df -h /private/tmp my-video/out
```

`--disallow-parallel-encoding` stores rendered frames before encoding. It lowers peak memory but increases temporary disk use, so use it on bounded chunks for long 4K/4K60 videos. Do not apply it to an unbounded long direct render without estimating scratch space first.

If shutdown returns `403`, `404`, times out, omits `server_stopped: true`, or the listener remains, stop before rendering. On `404`, first verify that `TTS_ENGINE` and `TTS_URL` point to the correct service; only then conclude that the running server predates the admin endpoint. Ask the user to restart the updated server or close it manually. Do not fall back to broad `pkill`, process-name matching, or killing an unrelated Python process.

### 8. Validate and Render Remotion

Composition IDs cannot contain `_`. A content directory named `EndDay_Final_4k` is rendered with composition id `EndDay-Final-4k`.

Still validation:

```bash
cd my-video
pnpm exec remotion still EndDay-Final-4k \
  --output /tmp/EndDay_Final_4k-still.png \
  --frame=320 \
  --scale=0.25 \
  --concurrency=1
```

Render stills one at a time. Never launch multiple 4K Remotion still/render commands through parallel tool calls or subagents.

Final render:

```bash
cd my-video
pnpm exec remotion render EndDay-Final-4k \
  out/EndDay_Final_4k.mp4 \
  --codec h264 \
  --crf 20 \
  --concurrency=1
```

For chunked 4K/4K60 rendering, the safe default is exactly one chunk process and one Remotion renderer:

```text
MAX_JOBS=1
RENDER_CONCURRENCY=1
```

For the low-memory or unattended path, pass `--disallow-parallel-encoding` to each bounded chunk render after the disk check. Do not reuse an older chunk script merely because it has `MAX_JOBS=1`: some existing scripts hard-code inner `--concurrency=2`, and older scripts may use up to four outer jobs. Make both levels configurable/default to `1`, and never run more than one chunk script at a time.

Chunk scripts should acquire an atomic `mkdir` render lock and release it with a shell `trap`; if the lock already exists, refuse to start another 4K render. Do not silently delete a lock without confirming its owner process is gone. Use a minimal temporary `--public-dir` containing only a symlink to the target content project so Remotion does not scan all historical 4K projects.

Resume only chunks that pass media validation and have a matching render signature; file existence alone is not sufficient.

Current FPS is defined in `my-video/src/lib/constants.ts` as `FPS = 30`.

## Recovery Rules

- TTS timeout: restart the selected backend, rerun synthesis; existing audio is skipped unless `--overwrite` is used.
- TTS shutdown is only a phase transition after audio QC. If later audio repair is required, restart the selected backend, regenerate only the missing/flagged lines, repeat RoleTone/QC, then run the pre-render memory gate again.
- Qwen3-TTS/Fish Speech shutdown `404`: the running server predates its admin endpoint. Do not start a memory-heavy render; have the user restart the updated server or close it manually.
- Shutdown accepted but exit confirmation times out: do not render and do not use a broad process kill. Report which backend did not stop cleanly.
- Character reference changed: rerun synthesis with `--speaker-id <id> --overwrite`, then rerun director.
- Background, sprite, placement, subtitle text changed: rerun director; no TTS needed unless `spokenText` changed.
- Remotion subtitle/style code changed: rerun still/render; no TTS or director needed unless baked images must change.
- Interrupted 4K/4K60 render: resume serially and only reuse chunks whose media specification and render signature still match the current timeline, assets, and Remotion source.

## Future Extensions

- Scene prompt fallback: if a requested background alias is missing, add a scene prompt field or TODO entry rather than blocking the whole script. Keep generated prompts in the script or a sidecar plan until the user adds assets.
- Richer staging: speaker zoom, inactive-character dimming, enter/exit, pan/zoom, BGM/SFX, and subtitle themes should initially be implemented in the director layer or as timeline styling metadata. Do not make Remotion infer story semantics.
