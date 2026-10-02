---
name: umamusume-video-pipeline
description: "Create, review, repair, and resume this repository's local Uma Musume story videos. Use for draft-to-script adaptation, per-line Fish Speech or Qwen3-TTS, audio fingerprint checks and selective repair, RoleTone QC, Director content, subtitle preflight, key-frame review, and safe Remotion rendering."
---

# Umamusume Video Pipeline

## Entry Point and Scope

Run commands from the repository root. Prefer `.venv/bin/python`; if the local environment is absent, use `uv run python` with the same arguments. Use the existing environment and assets; do not reinstall models or dependencies on every run.

The maintained orchestration entry point is `scripts/video_workflow.py`. Read [the workflow guide](../../../docs/video-workflow.md) before using it, including its legacy-audio and repair limitations. Use `--help` for current flags. Do not write per-story tools for operations already provided here:

| Task | Fixed entry point |
| --- | --- |
| Find a line by MP4 time, text, speaker, or ID; extract a listening clip | `video_workflow.py locate` |
| Allocate a new insertion ID without renumbering old lines | `video_workflow.py new-id` |
| Prepare, generate, compare, and apply selected voice candidates | `video_workflow.py repair create/generate/compare/apply` |
| Audit voice inputs/output hashes without contacting TTS | `synthesize_script.py --audit` |
| Measure subtitles in Chromium; render stills by line ID | `video_workflow.py subtitles/preview` |
| Initialize parameters, advance stages, record completed review | `video_workflow.py init/run/approve` |

Architecture remains:

```text
draft MD -> Agent-authored script JSON -> WAV + .wav.tts.json -> audio QC
         -> Director images/MP3/timeline -> subtitle/still review -> render_4k60.sh -> MP4
```

The Agent handles adaptation, character voice, translation, costumes, and staging. Remotion only renders the timeline; do not move story semantics into it or redesign the JSON scene protocol. Keep current RoleTone rules. Generated drafts, configurations, candidates, receipts, reports, and media belong in ignored working directories; do not require Git to preserve them.

## Script and Asset Preparation

Confirm the exact requested draft exists and read it before adapting. Preserve dialogue and important exchanges when requested; there is no fixed 35–50-line target. Keep narration restrained according to the user's request. Preserve supplied names, forms of address, and self-references; Chinese subtitles should not accidentally retain Japanese nicknames or TTS tags.

Check local `characters/`, `backgrounds/`, and `scripts/background_catalog.json`. Use the user's curated reference audio, not an arbitrary alternative. The synthesizer chooses `reference.mp3` before `reference.wav`; flag ambiguity if the intended new reference is not the file that will be selected. Each voiced speaker needs a matching asset directory and nonempty `reference_jp.txt`. Use exact directory case: current trainer/narration assets commonly use `Trainer` and `Voice-over`, not an assumed lowercase alias. List backgrounds with:

```bash
.venv/bin/python scripts/director.py list-backgrounds
```

Use uniforms and original racing outfits where the scene supports them, keeping clothing and staging consistent within a continuous scene. Keep background and participant slots stable through multi-person exchanges; changing the speaking partner is not itself a scene change. Prepare suitable missing background assets when in scope, but do not substitute an unrelated location merely to get a render.

### Existing JSON Contract

Prefer a top-level object with `projectId`, `title`, and `lines`; existing `scenes[].lines` scripts remain supported. Each line uses:

- `id`: unique, stable logical ID. Never renumber existing lines after an insertion. Keep each line's audio path unique.
- `type`: `dialogue` or `narration`.
- `speakerId`: the real character directory ID, including case.
- `background`: catalog alias or supported local path.
- `characters`: visible sprites, with stable `slot` values `left`, `right`, `center_left`, `center_right`, or `center`; `spriteScale` is only for base framing.
- `audio`: output WAV path for any voiced line, including trainer/narration if appropriate.
- `spokenText`: actual Japanese TTS text, including intentional Fish style tags.
- `subtitleJa`, `subtitleZh`: clean bilingual display text; explicit line breaks are allowed.
- `showSpeaker: false`: when appropriate for trainer or other non-visual speakers.

Director automatically enlarges/brightens the active character and dims other visible characters. Do not encode per-line active-speaker zoom by changing base sprite scale. Narration is neutral framing.

```bash
.venv/bin/python scripts/video_workflow.py new-id --prefix story
```

Use the returned ID only for the inserted line. The tools reject duplicates but do not repair old builders that regenerate sequential IDs; preserve IDs when editing those builders too.

### Fish Speech Text

Keep emotion tags in `spokenText`, not subtitles. Do not manually add `<|speaker:0|>`; the client adds it. Prefer one or two tone tags:

| Intent | Example S2-Pro tag |
| --- | --- |
| Bright / cheerful | `[bright and cheerful tone]` |
| Cute delight | `[delighted][pitch up]` |
| Excited, without forced shouting | `[excited]` |
| Confident / teasing | `[confident]` / `[playful tone]` |
| Gentle | `[soft tone]` |
| Hurried / surprised | `[in a hurry tone]` / `[surprised][pitch up]` |

Avoid excessive volume tags. If delivery drifts, first remove extra tags or lower sampling relative to that attempt. Global `--fish-style` is a fallback when text lacks a leading tag; per-line `fishStyle`, `fishStyleTag`, and `fishStyleSyntax` remain supported. S2-Pro uses bracket syntax (`s2`); S1-mini may require `s1`. Do not assume all backends accept Fish-specific flags.

## New 4K60 Production

### 1. Establish One Production Configuration

Select the backend explicitly; never infer it from a port. Fish Speech is the current default choice unless the user selects Qwen3-TTS. Keep the selected backend, URL, model identity, and generation parameters consistent through the run and rendering shutdown gate.

`--model-id` is an operator-declared checkpoint/version label, not automatic proof of the server's weights. Base it on the supplied launch configuration or known local setup. Keep it stable across resumes; update it when weights, server defaults, or relevant server implementation change. Do not invent a verified version when the model is unknown.

Check the selected service before sending generation requests, not merely to locate lines, audit files, preview, or resume an already-audio-complete render:

```bash
.venv/bin/python -m my_tts.cli fish health --base-url http://127.0.0.1:8002 --timeout 10
```

For Qwen3-TTS use `qwen health` and the actual selected URL instead. If unavailable or timing out, ask the user to start/restart that backend; do not switch voices or shut down unrelated services.

Example configuration after creating `draft/endday_final_script.json` with project `EndDay_Final_4k60`:

```bash
.venv/bin/python scripts/video_workflow.py init \
  --script draft/endday_final_script.json --output draft/endday.workflow.json \
  --tts-engine fishspeech --tts-url http://127.0.0.1:8002 --model-id s2-pro-local-v1
```

These names and the model label are examples, not targets to reuse for a different story. `init` refuses to overwrite an existing configuration; inspect and reuse/edit it for a resume. Its default `tts.args` are timeout 900, batch size 4, temperature 0.7, top-p 0.8, max-new-tokens 512. Fish S2-Pro usually benefits from batch size 2–4. Qwen supports its own language/sampling/subtalker flags. Adjust the configuration before generating; avoid changing global settings just to fix one line.

### 2. Generate Audio and Review QC

```bash
.venv/bin/python scripts/video_workflow.py run --config draft/endday.workflow.json --until audio
```

This generates missing audio using fingerprint-aware reuse and runs existing objective checks plus offline RoleTone. Reports are under `draft/endday.workflow.work/audio_qc/`, not the standalone `audio_qc.py` default directory. Read both `qc_report.json` and `retry_plan.json`.

`run` stopping at a review gate is expected, not a failure or completion of the requested MP4. Review scores, objective errors, durations, reference selection, and the flagged lines; listen when available/needed. Short clips can produce unreliable low RoleTone scores: do not regenerate solely on a low short-clip score, or claim to have listened when only inspecting measurements. Use the selective repair path below for genuinely bad audio. Start with one or two targeted attempts; if still unresolved, report the specific lines and seek direction instead of retrying indefinitely or lowering scoring rules.

```bash
.venv/bin/python scripts/video_workflow.py approve --config draft/endday.workflow.json --stage audio
```

`approve` records a completed review; it does not perform one. Within an authorized end-to-end production task, the Agent can complete the relevant checks and record approval, then continue without asking for an extra user confirmation at every stage. If the user explicitly wants to listen first, honor that pause. Use `--accept-warnings` only after assessing the actual warnings and documenting why they are acceptable. Objective errors or failed RoleTone scoring must be resolved; the tool will not approve them.

### 3. Build, Check Subtitles, and Inspect Key Frames

```bash
.venv/bin/python scripts/video_workflow.py run --config draft/endday.workflow.json --until preview
```

This runs Director at 3840×2160 with a 100 ms sentence gap, measures the shared subtitle component in Chromium, and renders serial previews of first/middle/last lines, layout changes, and subtitle warnings. Inspect `draft/endday.workflow.work/subtitles.json` and `stills/`. Warnings include overflow, short final rows, excess height/rows, Chinese text containing kana, and possible TTS tags. They are review hints, not automatic translation/line-break edits.

Check backgrounds, costume/slot continuity, character cropping, subtitle names and line breaks. If only subtitles need edits, update the main script and rerun this stage; this does not invalidate TTS. Director incrementally reuses images/audio, and the workflow invalidates changed visual approvals. Do not hand-edit cached timeline or review hashes to bypass a check.

```bash
.venv/bin/python scripts/video_workflow.py approve --config draft/endday.workflow.json --stage visual
```

Use `--accept-warnings` only after inspecting flagged frames that are intentionally retained. Do not overwrite the workflow-owned report or preview directory with unrelated manual checks; those files are hash-bound to approval. Put additional ad hoc previews in a separate output directory.

### 4. Render and Verify

```bash
.venv/bin/python scripts/video_workflow.py run --config draft/endday.workflow.json --until render
```

With both reviews current, this delegates to `scripts/render_4k60.sh`. Do not report success merely because `run` exited with code 0: it may have stopped at a gate. Require successful final render/media verification and identify the actual MP4 delivered.

The wrapper owns the repository render lock, minimal one-time bundle, static-frame reuse, one-time timeline audio assembly, signed chunk resume, final mux/decode checks, and safety gates. Do not create another final-render/assembly implementation.

## Locating and Selectively Repairing Audio

Read the repair examples and limitations in [the workflow guide](../../../docs/video-workflow.md) for this mode. Locate before selecting targets:

```bash
.venv/bin/python scripts/video_workflow.py locate \
  --script draft/endday_final_script.json --at 01:23.5 \
  --qc-report draft/endday.workflow.work/audio_qc/qc_report.json
```

Timestamps include the default one-second title intro. An old MP4 needs its matching timeline (`--timeline` if necessary). `--text`, `--line-id`, and `--speaker-id` are alternatives; a single match can be exported with `--video ... --clip ...`. Inspect stale-report indicators rather than treating an old pass score as current.

```bash
.venv/bin/python scripts/video_workflow.py repair create \
  --script draft/endday_final_script.json --config draft/endday.workflow.json \
  --line-id kb001 --work-dir draft/endday_retry_01
.venv/bin/python scripts/video_workflow.py repair generate --work-dir draft/endday_retry_01 -- \
  --tts-engine fishspeech --fish-tts-url http://127.0.0.1:8002 --model-id s2-pro-local-v1 \
  --timeout 900 --batch-size 4 --temperature 0.7 --top-p 0.8 --max-new-tokens 512
.venv/bin/python scripts/video_workflow.py repair compare --work-dir draft/endday_retry_01
```

Use `--speaker-id` instead of `--line-id` for a character-wide voice replacement; update the intended reference before creating the repair. Add `--patch draft/fixes.json` to `create` for an ID-to-edits object containing changed `spokenText`, supported style fields, or matching subtitles. Keep original script/WAVs unchanged during candidate preparation and comparison. Review `draft/endday_retry_01/candidates/comparison_report.json`, then:

```bash
.venv/bin/python scripts/video_workflow.py repair apply --work-dir draft/endday_retry_01
```

Only approved replacements update the original WAVs and corresponding script edits, with backups and receipts preserved. The original production request and actual retry settings are both recorded for accepted candidates. Pass the production `--config` especially for legacy audio without receipts. Confirm the report's applied count; a high-scoring original may cause a new candidate to be kept rather than applied. Do not silently override this policy or pretend a requested content correction was applied when it was not.

Use a new repair directory for another attempt or when bound inputs change. If the production configuration/backend/model changes, restart the repair with that configuration rather than mixing unrecorded defaults. Rerun the production workflow after application so audio QC, duration-dependent timing, and visuals are current. Restart TTS first if an earlier final render stopped it.

## Fingerprints and Existing Productions

Each new WAV receives a `.wav.tts.json` receipt. File existence alone is not enough to reuse audio. Audit with the same backend/model and complete production arguments (including request mode and batch size):

```bash
.venv/bin/python scripts/synthesize_script.py \
  --script draft/endday_final_script.json --tts-engine fishspeech \
  --fish-tts-url http://127.0.0.1:8002 --model-id s2-pro-local-v1 \
  --timeout 900 --batch-size 4 --temperature 0.7 --top-p 0.8 --max-new-tokens 512 \
  --audit --audit-output draft/endday_audio_audit.json
```

Check the report; `--audit` is read-only for audio and does not fail its exit code just because stale entries exist.

- `fresh` / `accepted`: reusable for the current request. Accepted retries retain their actual generation provenance.
- `missing`: synthesize the missing line normally.
- `unverified`: old audio or incomplete generation identity. Do not fake a verified receipt. When the user has asked to keep existing audio, explicitly add `--reuse-unverified` to the configuration's `tts.args`, explain this limitation, and retain the files. Otherwise establish that decision before continuing; do not regenerate the entire legacy project merely to create receipts.
- `stale` / `modified`: inputs or WAV bytes changed. Investigate/selectively repair. `--reuse-unverified` cannot bypass these states; do not delete receipts or use whole-project `--overwrite` to suppress the error.

Changing only `subtitleJa`, `subtitleZh`, or other display fields does not invalidate voice generation. Changing `spokenText`, the selected reference audio/text, model identity, or synthesis settings does. Preserve unrelated WAVs when repairing a subset. Direct `--overwrite` is a deliberate replacement, not the normal recovery path; prefer candidates and reviewed application.

## Targeted Visual Work and Render Recovery

For subtitle/asset-only work, use the existing script and low-level Director plus the fixed review tools; do not force a legacy audio migration or new TTS run just to change captions:

```bash
.venv/bin/python scripts/director.py build \
  --script draft/endday_final_script.json --project EndDay_Final_4k60 \
  --width 3840 --height 2160 --line-gap-ms 100
.venv/bin/python scripts/video_workflow.py subtitles \
  --script draft/endday_final_script.json --project EndDay_Final_4k60 \
  --output draft/endday_subtitles.json
.venv/bin/python scripts/video_workflow.py preview \
  --script draft/endday_final_script.json --project EndDay_Final_4k60 \
  --line-id kb001 --output draft/endday_extra_stills
```

The preview tool computes 60fps/intro-adjusted frames and bundles only the selected project. Render stills serially. A global Remotion subtitle style change needs new preflight/stills/render, but not TTS or Director unless script/baked content also changed. Director owns `.director-manifest.json`; do not force `--overwrite` for ordinary incremental updates.

After required review, or to resume an interrupted render with already-validated content:

```bash
scripts/render_4k60.sh --project EndDay_Final_4k60 \
  --tts-engine fishspeech --tts-url http://127.0.0.1:8002
```

Preserve the recorded backend/URL and media parameters. No TTS restart is needed just to resume matching render chunks; an already-refused connection is handled by the wrapper. Workflow configuration currently targets 4K60/100 ms gaps; for explicitly different dimensions/gaps use the appropriate existing lower-level tools, not a fictitious workflow flag. For 1080p Director use `--width 1920 --height 1080`; final 4K60 always uses the unified wrapper.

### Non-negotiable Render Safety

- Do not shut down TTS while synthesis, QC, or retry work remains. Never infer the backend from a port.
- The final wrapper checks the selected loopback backend, requests graceful shutdown, verifies `server_stopped: true` and that its listener is gone. Only connection-refused means already stopped. A timeout, old shutdown `404`, missing confirmation, or live listener is a real blocker; ask for the selected server to be restarted/closed, never use broad `pkill`.
- Require valid `memory_pressure -Q` before/after shutdown and at least 40% free pressure for one worker, plus 10 percentage points per extra active worker; require 20 GiB scratch per outer job on both temp/output volumes. Do not bypass these gates.
- Default to one outer job and one inner render worker. If the user asks for parallel chunks and gates pass, start with two outer jobs using the existing renderer options; do not launch parallel 4K still/render processes ad hoc.
- Keep Remotion's 30fps preview fallback. Final and fixed-tool stills explicitly use `renderFps: 60`; composition IDs replace unsupported project characters such as underscores with hyphens.

For recovery, rerun the last relevant fixed stage with unchanged parameters. Do not treat review gates as errors, change thresholds to manufacture a pass, or announce a finished MP4 before final verification.
