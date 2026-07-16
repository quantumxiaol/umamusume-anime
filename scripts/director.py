from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance


DEFAULT_BACKGROUND_CATALOG = Path("scripts") / "background_catalog.json"
DEFAULT_CHARACTERS_ROOT = Path("characters")
DEFAULT_CONTENT_ROOT = Path("my-video") / "public" / "content"
DEFAULT_CANVAS_SIZE = (1920, 1080)
DEFAULT_LINE_DURATION_MS = 3000
DEFAULT_SPRITE_SCALE = 0.92
ACTIVE_SPRITE_SCALE_MULTIPLIER = 1.04
INACTIVE_SPRITE_SCALE_MULTIPLIER = 0.97
INACTIVE_SPRITE_BRIGHTNESS = 0.68
MAX_SUBTITLE_LINES = 2
MAX_SUBTITLE_DISPLAY_WIDTH = 108
DIRECTOR_MANIFEST_FILENAME = ".director-manifest.json"
DIRECTOR_MANIFEST_VERSION = 1
VISUAL_PIPELINE_VERSION = 1
AUDIO_PIPELINE_VERSION = 1
AUDIO_MP3_QUALITY = 2


@dataclass(frozen=True)
class ResolvedSprite:
    source: Path
    source_sha256: str
    base_scale: float
    scale_multiplier: float
    brightness: float
    x_mode: str
    x_value: int | str
    y_mode: str
    y_value: int


@dataclass(frozen=True)
class VisualPlan:
    background: Path
    background_sha256: str
    sprites: tuple[ResolvedSprite, ...]
    canvas_size: tuple[int, int]

    def fingerprint_inputs(self) -> dict[str, Any]:
        return {
            "pipelineVersion": VISUAL_PIPELINE_VERSION,
            "canvas": {"width": self.canvas_size[0], "height": self.canvas_size[1]},
            "background": {"sha256": self.background_sha256},
            "sprites": [
                {
                    "sha256": sprite.source_sha256,
                    "baseScale": sprite.base_scale,
                    "scaleMultiplier": sprite.scale_multiplier,
                    "brightness": sprite.brightness,
                    "x": {"mode": sprite.x_mode, "value": sprite.x_value},
                    "y": {"mode": sprite.y_mode, "value": sprite.y_value},
                }
                for sprite in self.sprites
            ],
            "compositor": {
                "backgroundFit": "cover-lanczos",
                "spriteResize": "height-lanczos",
                "layerOrder": "script-order",
                "output": "rgb-png",
            },
        }

    def source_details(self, *, repo_root: Path) -> dict[str, Any]:
        return {
            "background": display_path(self.background, repo_root=repo_root),
            "sprites": [display_path(sprite.source, repo_root=repo_root) for sprite in self.sprites],
        }


class DirectorError(RuntimeError):
    pass


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "build":
            build_project(args)
            return 0
        if args.command == "list-backgrounds":
            list_backgrounds(args)
            return 0
        parser.error(f"unsupported command: {args.command}")
    except DirectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="director",
        description="Build Remotion-compatible still frames and timeline from a structured script.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build images and timeline for my-video.")
    build.add_argument("--script", required=True, help="Structured script JSON.")
    build.add_argument("--project", help="Output project slug. Defaults to script projectId or file stem.")
    build.add_argument("--content-root", default=str(DEFAULT_CONTENT_ROOT), help="Remotion content root.")
    build.add_argument("--background-catalog", default=str(DEFAULT_BACKGROUND_CATALOG), help="Background catalog JSON.")
    build.add_argument("--characters-root", default=str(DEFAULT_CHARACTERS_ROOT), help="Character assets root.")
    build.add_argument("--width", type=int, default=DEFAULT_CANVAS_SIZE[0], help="Output frame width.")
    build.add_argument("--height", type=int, default=DEFAULT_CANVAS_SIZE[1], help="Output frame height.")
    build.add_argument(
        "--line-gap-ms",
        type=int,
        default=0,
        help="Silent subtitle-free gap to insert between script lines, in milliseconds.",
    )
    build.add_argument("--overwrite", action="store_true", help="Overwrite generated images and copied audio.")
    build.add_argument(
        "--no-auto-focus",
        action="store_true",
        help="Disable automatic active-speaker scale/brightness styling.",
    )

    list_bg = subparsers.add_parser("list-backgrounds", help="Print known background aliases.")
    list_bg.add_argument("--background-catalog", default=str(DEFAULT_BACKGROUND_CATALOG), help="Background catalog JSON.")

    return parser


def build_project(args: argparse.Namespace) -> None:
    script_path = Path(args.script).expanduser().resolve()
    if not script_path.exists():
        raise DirectorError(f"script not found: {script_path}")

    repo_root = Path.cwd()
    script = read_json(script_path)
    project = args.project or str(script.get("projectId") or script_path.stem)
    if not project:
        raise DirectorError("project slug is empty")
    validate_output_id(project)

    catalog = read_json(Path(args.background_catalog))
    lines = flatten_lines(script)
    if not lines:
        raise DirectorError("script has no lines")
    if args.line_gap_ms < 0:
        raise DirectorError("--line-gap-ms must be >= 0")
    canvas_size = (args.width, args.height)

    project_root = Path(args.content_root) / project
    image_dir = project_root / "images"
    audio_dir = project_root / "audio"
    image_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    previous_manifest = load_director_manifest(project_root)
    previous_owned = collect_previous_owned_files(
        project_root=project_root,
        previous_manifest=previous_manifest,
    )
    previous_lines = manifest_mapping(previous_manifest, "lines")
    previous_frames = manifest_mapping(previous_manifest, "frames")

    timeline: dict[str, Any] = {
        "shortTitle": str(script.get("title") or project),
        "width": canvas_size[0],
        "height": canvas_size[1],
        "elements": [],
        "text": [],
        "audio": [],
    }
    descriptor: dict[str, Any] = {
        "shortTitle": str(script.get("title") or project),
        "content": [],
    }
    characters_root = Path(args.characters_root)
    speaker_label_cache: dict[str, str] = {}
    file_hash_cache: dict[tuple[str, int, int], str] = {}
    handled_visuals: set[str] = set()
    manifest_lines: dict[str, Any] = {}
    manifest_frames: dict[str, Any] = {}
    owned_images: set[str] = set()
    owned_audio: set[str] = set()

    line_ids: set[str] = set()
    for index, line in enumerate(lines, start=1):
        line_id = str(line.get("id") or f"l{index:03d}")
        validate_output_id(line_id)
        if line_id in line_ids:
            raise DirectorError(f"duplicate line id: {line_id}")
        line_ids.add(line_id)

    current_ms = 0
    for index, line in enumerate(lines, start=1):
        line_id = str(line.get("id") or f"l{index:03d}")
        previous_line = previous_lines.get(line_id) if isinstance(previous_lines.get(line_id), dict) else {}

        audio_path = audio_dir / f"{line_id}.mp3"
        audio_plan = build_audio_plan(
            line=line,
            repo_root=repo_root,
            file_hash_cache=file_hash_cache,
        )
        audio_record: dict[str, Any] | None = None
        if audio_plan is not None:
            owned_audio.add(audio_path.name)
            previous_audio = previous_line.get("audio") if isinstance(previous_line, dict) else None
            audio_is_current = (
                not args.overwrite
                and cached_audio_is_current(
                    previous_audio,
                    expected_fingerprint=audio_plan["fingerprint"],
                    destination=audio_path,
                    file_hash_cache=file_hash_cache,
                )
            )
            if not audio_is_current:
                prepare_audio(
                    line=line,
                    destination=audio_path,
                    repo_root=repo_root,
                    overwrite=True,
                    source=audio_plan["source"],
                )

            if audio_is_current and isinstance(previous_audio.get("durationMs"), int):
                duration_ms = max(1, int(previous_audio["durationMs"]))
            else:
                duration_ms = audio_duration_ms(audio_path)
            audio_record = {
                "fingerprint": audio_plan["fingerprint"],
                "inputs": audio_plan["inputs"],
                "source": display_path(audio_plan["source"], repo_root=repo_root),
                "output": audio_path.name,
                "outputSize": audio_path.stat().st_size,
                "outputSha256": file_sha256(audio_path, cache=file_hash_cache),
                "durationMs": duration_ms,
            }
        else:
            duration_ms = estimated_duration_ms(line)

        visual_plan = resolve_visual_plan(
            line=line,
            catalog=catalog,
            characters_root=characters_root,
            repo_root=repo_root,
            canvas_size=canvas_size,
            auto_focus=not args.no_auto_focus,
            file_hash_cache=file_hash_cache,
        )
        visual_inputs = visual_plan.fingerprint_inputs()
        visual_fingerprint = stable_fingerprint(visual_inputs)
        image_url = f"frame-{visual_fingerprint}"
        image_path = image_dir / f"{image_url}.png"
        owned_images.add(image_path.name)

        if visual_fingerprint not in handled_visuals:
            previous_frame = previous_frames.get(image_url)
            frame_is_current = (
                not args.overwrite
                and cached_frame_is_current(
                    previous_frame,
                    expected_fingerprint=visual_fingerprint,
                    destination=image_path,
                )
            )
            if not frame_is_current:
                compose_resolved_frame(plan=visual_plan, destination=image_path)
            handled_visuals.add(visual_fingerprint)

        manifest_frames[image_url] = {
            "fingerprint": visual_fingerprint,
            "inputs": visual_inputs,
            "sources": visual_plan.source_details(repo_root=repo_root),
            "output": image_path.name,
            "outputSize": image_path.stat().st_size,
            "outputMtimeNs": image_path.stat().st_mtime_ns,
        }

        start_ms = current_ms
        end_ms = current_ms + duration_ms
        next_start_ms = end_ms + (args.line_gap_ms if index < len(lines) else 0)
        timeline["elements"].append(
            {
                "startMs": start_ms,
                "endMs": next_start_ms,
                "imageUrl": image_url,
                "enterTransition": "none",
                "exitTransition": "none",
            }
        )
        if audio_record is not None:
            timeline["audio"].append({"startMs": start_ms, "endMs": end_ms, "audioUrl": line_id})

        subtitle_element = build_subtitle_element(
            line=line,
            line_id=line_id,
            start_ms=start_ms,
            end_ms=end_ms,
            characters_root=characters_root,
            speaker_label_cache=speaker_label_cache,
        )
        if subtitle_element is not None:
            timeline["text"].append(subtitle_element)

        descriptor["content"].append(
            {
                "uid": line_id,
                "text": descriptor_text(line),
                "imageDescription": str(line.get("background") or ""),
                "durationMs": duration_ms,
            }
        )
        manifest_line: dict[str, Any] = {
            "visualFingerprint": visual_fingerprint,
            "imageUrl": image_url,
            "durationMs": duration_ms,
        }
        if audio_record is not None:
            manifest_line["audio"] = audio_record
        manifest_lines[line_id] = manifest_line
        current_ms = next_start_ms

    write_json(project_root / "timeline.json", timeline)
    write_json(project_root / "descriptor.json", descriptor)
    manifest = {
        "version": DIRECTOR_MANIFEST_VERSION,
        "project": project,
        "settings": {
            "width": canvas_size[0],
            "height": canvas_size[1],
            "autoFocus": not args.no_auto_focus,
            "lineGapMs": args.line_gap_ms,
        },
        "lines": manifest_lines,
        "frames": manifest_frames,
        "ownedFiles": {
            "images": sorted(owned_images),
            "audio": sorted(owned_audio),
        },
    }
    # Publish a transitional ownership union before pruning. If the process is
    # interrupted during cleanup, the next run still knows which stale outputs
    # Director owned and can finish removing them safely.
    prune_manifest = dict(manifest)
    prune_manifest["ownedFiles"] = {
        "images": sorted(previous_owned.get("images", set()) | owned_images),
        "audio": sorted(previous_owned.get("audio", set()) | owned_audio),
    }
    manifest_path = project_root / DIRECTOR_MANIFEST_FILENAME
    write_json(manifest_path, prune_manifest)
    prune_unreferenced_generated_files(
        image_dir=image_dir,
        audio_dir=audio_dir,
        previous_owned=previous_owned,
        current_owned={"images": owned_images, "audio": owned_audio},
    )
    write_json(manifest_path, manifest)
    print(f"project:    {project}")
    print(f"images:     {image_dir}")
    print(f"audio:      {audio_dir}")
    print(f"timeline:   {project_root / 'timeline.json'}")


def list_backgrounds(args: argparse.Namespace) -> None:
    catalog = read_json(Path(args.background_catalog))
    for key, item in sorted(catalog.items()):
        source = item.get("source", "")
        name_zh = item.get("name_zh", "")
        variant = item.get("variant", "")
        print(f"{key}\t{name_zh}\t{variant}\t{source}")


def flatten_lines(script: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(script.get("lines"), list):
        return [dict(line) for line in script["lines"] if isinstance(line, dict)]

    lines: list[dict[str, Any]] = []
    for scene in script.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        scene_defaults = {
            "background": scene.get("background"),
            "characters": scene.get("characters"),
        }
        for line in scene.get("lines", []):
            if isinstance(line, dict):
                merged = {key: value for key, value in scene_defaults.items() if value is not None}
                merged.update(line)
                lines.append(merged)
    return lines


def compose_frame(
    *,
    line: dict[str, Any],
    destination: Path,
    catalog: dict[str, Any],
    characters_root: Path,
    repo_root: Path,
    canvas_size: tuple[int, int],
    auto_focus: bool,
) -> None:
    plan = resolve_visual_plan(
        line=line,
        catalog=catalog,
        characters_root=characters_root,
        repo_root=repo_root,
        canvas_size=canvas_size,
        auto_focus=auto_focus,
        file_hash_cache={},
    )
    compose_resolved_frame(plan=plan, destination=destination)


def resolve_visual_plan(
    *,
    line: dict[str, Any],
    catalog: dict[str, Any],
    characters_root: Path,
    repo_root: Path,
    canvas_size: tuple[int, int],
    auto_focus: bool,
    file_hash_cache: dict[tuple[str, int, int], str],
) -> VisualPlan:
    background_path = resolve_background_path(line.get("background"), catalog, repo_root)
    sprites: list[ResolvedSprite] = []
    for character in character_specs_for_line(line):
        sprite_path = resolve_sprite_path(line=character, characters_root=characters_root, repo_root=repo_root)
        style = character_focus_style(line=line, character=character, auto_focus=auto_focus)
        if "spriteX" in character:
            x_mode = "absolute"
            x_value: int | str = int(character["spriteX"])
        else:
            x_mode = "slot"
            x_value = str(character.get("slot") or "center")
        if "spriteY" in character:
            y_mode = "absolute"
            y_value = int(character["spriteY"])
        else:
            y_mode = "bottom-offset"
            y_value = int(character.get("spriteBottomOffset", 24))
        sprites.append(
            ResolvedSprite(
                source=sprite_path,
                source_sha256=file_sha256(sprite_path, cache=file_hash_cache),
                base_scale=float(character.get("spriteScale", DEFAULT_SPRITE_SCALE)),
                scale_multiplier=float(style["scale_multiplier"]),
                brightness=float(style["brightness"]),
                x_mode=x_mode,
                x_value=x_value,
                y_mode=y_mode,
                y_value=y_value,
            )
        )
    return VisualPlan(
        background=background_path,
        background_sha256=file_sha256(background_path, cache=file_hash_cache),
        sprites=tuple(sprites),
        canvas_size=canvas_size,
    )


def compose_resolved_frame(*, plan: VisualPlan, destination: Path) -> None:
    with Image.open(plan.background) as background_image:
        canvas = crop_cover(background_image.convert("RGBA"), plan.canvas_size)

    for resolved in plan.sprites:
        with Image.open(resolved.source) as sprite_image:
            sprite = sprite_image.convert("RGBA")
        sprite = apply_sprite_style(
            sprite=sprite,
            style={"brightness": resolved.brightness},
        )
        scale = resolved.base_scale * resolved.scale_multiplier
        sprite = resize_sprite(sprite, target_height=int(plan.canvas_size[1] * scale))
        if resolved.x_mode == "absolute":
            x = int(resolved.x_value)
        else:
            x = slot_x(
                slot=str(resolved.x_value),
                sprite_width=sprite.width,
                canvas_width=plan.canvas_size[0],
            )
        if resolved.y_mode == "absolute":
            y = resolved.y_value
        else:
            y = plan.canvas_size[1] - sprite.height + resolved.y_value
        canvas.alpha_composite(sprite, (x, y))

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.tmp{destination.suffix}")
    try:
        canvas.convert("RGB").save(temporary, quality=95)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_background_path(value: Any, catalog: dict[str, Any], repo_root: Path) -> Path:
    if not value:
        raise DirectorError("line is missing background")
    key = str(value)
    if key in catalog:
        source = catalog[key].get("source")
        if not source:
            raise DirectorError(f"background catalog item has no source: {key}")
        return resolve_existing_path(source, repo_root=repo_root, label=f"background {key}")
    return resolve_existing_path(key, repo_root=repo_root, label="background")


def resolve_sprite_path(*, line: dict[str, Any], characters_root: Path, repo_root: Path) -> Path:
    if line.get("sprite"):
        return resolve_existing_path(str(line["sprite"]), repo_root=repo_root, label="sprite")

    speaker_value = line.get("speakerId") or line.get("id") or line.get("characterId")
    if not speaker_value:
        raise DirectorError("character is missing speakerId/id/characterId")
    speaker = normalize_speaker_id(str(speaker_value))
    character_dir = characters_root / speaker
    if not character_dir.exists() and speaker.startswith("uma_"):
        character_dir = characters_root / speaker.removeprefix("uma_")
    if not character_dir.exists():
        raise DirectorError(f"character directory not found for speakerId={speaker}: {character_dir}")

    variant = str(line.get("spriteVariant") or "ZF").upper()
    candidates = sorted(character_dir.glob(f"{variant}_*.png"))
    if not candidates and variant != "ZF":
        candidates = sorted(character_dir.glob("ZF_*.png"))
    if not candidates:
        candidates = sorted(character_dir.glob("*.png"))
    if not candidates:
        raise DirectorError(f"no sprite png found in {character_dir}")
    return candidates[0]


def character_specs_for_line(line: dict[str, Any]) -> list[dict[str, Any]]:
    characters = line.get("characters")
    if isinstance(characters, list):
        return [dict(character) for character in characters if isinstance(character, dict)]

    if line.get("showSpeaker") is False:
        return []

    speaker_id = line.get("speakerId")
    line_type = str(line.get("type") or "dialogue")
    if speaker_id and line_type != "narration":
        return [dict(line)]
    return []


def character_focus_style(*, line: dict[str, Any], character: dict[str, Any], auto_focus: bool) -> dict[str, float]:
    if not auto_focus or character.get("focus") == "neutral" or line.get("focus") == "neutral":
        return {"scale_multiplier": 1.0, "brightness": 1.0}

    active_speaker = normalize_speaker_id(str(line.get("speakerId") or "")).lower()
    character_speaker = normalize_speaker_id(str(character.get("speakerId") or character.get("id") or "")).lower()
    if not active_speaker or not character_speaker:
        return {"scale_multiplier": 1.0, "brightness": 1.0}

    if active_speaker in {"voice-over", "voice_over", "narrator"} or str(line.get("type") or "") == "narration":
        return {"scale_multiplier": 1.0, "brightness": 1.0}

    if active_speaker == character_speaker:
        return {"scale_multiplier": ACTIVE_SPRITE_SCALE_MULTIPLIER, "brightness": 1.0}
    return {"scale_multiplier": INACTIVE_SPRITE_SCALE_MULTIPLIER, "brightness": INACTIVE_SPRITE_BRIGHTNESS}


def apply_sprite_style(*, sprite: Image.Image, style: dict[str, float]) -> Image.Image:
    brightness = float(style.get("brightness", 1.0))
    if brightness == 1.0:
        return sprite

    alpha = sprite.getchannel("A")
    rgb = ImageEnhance.Brightness(sprite.convert("RGB")).enhance(brightness)
    styled = rgb.convert("RGBA")
    styled.putalpha(alpha)
    return styled


def character_position(
    *,
    character: dict[str, Any],
    sprite: Image.Image,
    canvas_size: tuple[int, int],
) -> tuple[int, int]:
    canvas_w, canvas_h = canvas_size
    if "spriteX" in character:
        x = int(character["spriteX"])
    else:
        slot = str(character.get("slot") or "center")
        x = slot_x(slot=slot, sprite_width=sprite.width, canvas_width=canvas_w)

    if "spriteY" in character:
        y = int(character["spriteY"])
    else:
        y = canvas_h - sprite.height + int(character.get("spriteBottomOffset", 24))
    return x, y


def slot_x(*, slot: str, sprite_width: int, canvas_width: int) -> int:
    if slot == "left":
        return round(canvas_width * 0.08)
    if slot == "right":
        return round(canvas_width * 0.92 - sprite_width)
    if slot == "center_left":
        return round(canvas_width * 0.30 - sprite_width / 2)
    if slot == "center_right":
        return round(canvas_width * 0.70 - sprite_width / 2)
    return round((canvas_width - sprite_width) / 2)


def normalize_speaker_id(value: str) -> str:
    return value.removeprefix("uma_").strip()


def crop_cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_w, target_h = size
    scale = max(target_w / image.width, target_h / image.height)
    resized = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - target_w) // 2)
    top = max(0, (resized.height - target_h) // 2)
    return resized.crop((left, top, left + target_w, top + target_h))


def resize_sprite(sprite: Image.Image, *, target_height: int) -> Image.Image:
    scale = target_height / sprite.height
    return sprite.resize((round(sprite.width * scale), target_height), Image.Resampling.LANCZOS)


def prepare_audio(
    *,
    line: dict[str, Any],
    destination: Path,
    repo_root: Path,
    overwrite: bool,
    source: Path | None = None,
) -> Path:
    audio_value = line.get("audio")
    if not audio_value:
        return destination
    if destination.exists() and not overwrite:
        return destination

    if source is None:
        source = resolve_existing_path(str(audio_value), repo_root=repo_root, label="audio")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.tmp{destination.suffix}")
    try:
        if source.suffix.lower() == ".mp3":
            if source.resolve() != destination.resolve():
                shutil.copy2(source, temporary)
                temporary.replace(destination)
        else:
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(source),
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    str(AUDIO_MP3_QUALITY),
                    str(temporary),
                ]
            )
            temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def build_audio_plan(
    *,
    line: dict[str, Any],
    repo_root: Path,
    file_hash_cache: dict[tuple[str, int, int], str],
) -> dict[str, Any] | None:
    audio_value = line.get("audio")
    if not audio_value:
        return None

    source = resolve_existing_path(str(audio_value), repo_root=repo_root, label="audio")
    if source.suffix.lower() == ".mp3":
        transcode: dict[str, Any] = {
            "mode": "copy",
            "sourceFormat": ".mp3",
            "outputFormat": ".mp3",
        }
    else:
        transcode = {
            "mode": "ffmpeg",
            "codec": "libmp3lame",
            "quality": AUDIO_MP3_QUALITY,
            "outputFormat": ".mp3",
        }
    inputs = {
        "pipelineVersion": AUDIO_PIPELINE_VERSION,
        "source": {
            "sha256": file_sha256(source, cache=file_hash_cache),
            "suffix": source.suffix.lower(),
        },
        "transcode": transcode,
    }
    return {
        "source": source,
        "inputs": inputs,
        "fingerprint": stable_fingerprint(inputs),
    }


def audio_duration_ms(path: Path) -> int:
    if not path.exists():
        return DEFAULT_LINE_DURATION_MS
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    try:
        duration_seconds = float(result.stdout.strip())
    except ValueError as exc:
        raise DirectorError(f"could not parse audio duration for {path}: {result.stdout}") from exc
    return max(1, round(duration_seconds * 1000))


def estimated_duration_ms(line: dict[str, Any]) -> int:
    text = str(line.get("spokenText") or line.get("subtitle") or line.get("subtitleZh") or "")
    return max(DEFAULT_LINE_DURATION_MS, len(text) * 180)


def build_subtitle_element(
    *,
    line: dict[str, Any],
    line_id: str,
    start_ms: int,
    end_ms: int,
    characters_root: Path,
    speaker_label_cache: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    kind = "narration" if str(line.get("type") or "").strip().lower() == "narration" else "dialogue"
    speaker_id = clean_text(line.get("speakerId"))
    speaker_label = resolve_speaker_label(
        line=line,
        characters_root=characters_root,
        cache=speaker_label_cache,
    )
    if kind == "dialogue" and not speaker_label:
        speaker_description = speaker_id or "<missing>"
        print(
            f"warning: line {line_id}: dialogue speaker has no label (speakerId={speaker_description})",
            file=sys.stderr,
        )

    subtitle_ja, subtitle_zh = structured_subtitle_text(line)
    if not subtitle_ja and not subtitle_zh:
        return None

    warn_if_subtitle_is_too_long(line_id=line_id, field="subtitleJa", text=subtitle_ja)
    warn_if_subtitle_is_too_long(line_id=line_id, field="subtitleZh", text=subtitle_zh)

    item: dict[str, Any] = {
        "id": line_id,
        "startMs": start_ms,
        "endMs": end_ms,
        "kind": kind,
        "position": "bottom",
    }
    if speaker_id:
        item["speakerId"] = speaker_id
    if speaker_label:
        item["speakerLabel"] = speaker_label
    if subtitle_ja:
        item["subtitleJa"] = subtitle_ja
    if subtitle_zh:
        item["subtitleZh"] = subtitle_zh
    return item


def structured_subtitle_text(line: dict[str, Any]) -> tuple[str, str]:
    subtitle_ja = clean_text(line.get("subtitleJa"))
    subtitle_zh = clean_text(line.get("subtitleZh"))
    if subtitle_ja or subtitle_zh:
        return subtitle_ja, subtitle_zh

    # Older scripts used one untyped subtitle field. spokenText is normally
    # Japanese, so keep the legacy text intact in the primary subtitle slot.
    fallback = clean_text(line.get("subtitle")) or clean_text(line.get("spokenText"))
    return fallback, ""


def resolve_speaker_label(
    *,
    line: dict[str, Any],
    characters_root: Path,
    cache: dict[str, str] | None = None,
) -> str:
    explicit_label = clean_text(line.get("speakerLabel"))
    if explicit_label:
        return explicit_label

    speaker_id = clean_text(line.get("speakerId"))
    config_label = character_config_label(
        speaker_id=speaker_id,
        characters_root=characters_root,
        cache=cache,
    )
    if config_label:
        return config_label
    return default_speaker_label(line)


def character_config_label(
    *,
    speaker_id: str,
    characters_root: Path,
    cache: dict[str, str] | None = None,
) -> str:
    if not speaker_id:
        return ""

    normalized_speaker_id = normalize_speaker_id(speaker_id)
    if cache is not None and normalized_speaker_id in cache:
        return cache[normalized_speaker_id]

    label = ""
    config_path = characters_root / normalized_speaker_id / "config.json"
    if config_path.is_file():
        try:
            label = clean_text(read_json(config_path).get("name_zh"))
        except DirectorError as exc:
            print(f"warning: could not read speaker label from {config_path}: {exc}", file=sys.stderr)

    if cache is not None:
        cache[normalized_speaker_id] = label
    return label


def descriptor_text(line: dict[str, Any]) -> str:
    subtitle_ja, subtitle_zh = structured_subtitle_text(line)
    return clean_text(line.get("spokenText")) or subtitle_ja or subtitle_zh


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def subtitle_display_width(text: str) -> int:
    return sum(
        0 if unicodedata.combining(character) else 2 if unicodedata.east_asian_width(character) in {"W", "F", "A"} else 1
        for character in text
        if character not in {"\r", "\n"}
    )


def warn_if_subtitle_is_too_long(*, line_id: str, field: str, text: str) -> None:
    if not text:
        return
    manual_lines = text.splitlines() or [text]
    display_width = subtitle_display_width(text)
    if len(manual_lines) <= MAX_SUBTITLE_LINES and display_width <= MAX_SUBTITLE_DISPLAY_WIDTH:
        return
    print(
        f"warning: line {line_id}: {field} is likely too long "
        f"(display width {display_width}, manual lines {len(manual_lines)}); text was kept unchanged",
        file=sys.stderr,
    )


def default_speaker_label(line: dict[str, Any]) -> str:
    if str(line.get("type") or "") == "narration":
        return "旁白"
    speaker_id = str(line.get("speakerId") or "")
    if speaker_id.lower() == "trainer":
        return "训练员"
    if speaker_id.lower() in {"voice-over", "voice_over", "narrator"}:
        return "旁白"
    return ""


def resolve_existing_path(path_value: str, *, repo_root: Path, label: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    path = path.resolve()
    if not path.exists():
        raise DirectorError(f"{label} not found: {path}")
    return path


def validate_output_id(value: str) -> None:
    if not value or value in {".", ".."} or Path(value).name != value or "/" in value or "\\" in value:
        raise DirectorError(f"line id is not a safe output basename: {value!r}")


def stable_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path, *, cache: dict[tuple[str, int, int], str]) -> str:
    resolved = path.resolve()
    stat = resolved.stat()
    key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    cached = cache.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    cache[key] = value
    return value


def display_path(path: Path, *, repo_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def load_director_manifest(project_root: Path) -> dict[str, Any] | None:
    path = project_root / DIRECTOR_MANIFEST_FILENAME
    if not path.exists():
        return None
    try:
        manifest = read_json(path)
    except DirectorError as exc:
        print(f"warning: ignoring unreadable director manifest {path}: {exc}", file=sys.stderr)
        return None
    if manifest.get("version") != DIRECTOR_MANIFEST_VERSION:
        print(
            f"warning: ignoring unsupported director manifest version in {path}: {manifest.get('version')!r}",
            file=sys.stderr,
        )
        return None
    return manifest


def manifest_mapping(manifest: dict[str, Any] | None, key: str) -> dict[str, Any]:
    if manifest is None:
        return {}
    value = manifest.get(key)
    return value if isinstance(value, dict) else {}


def cached_frame_is_current(
    record: Any,
    *,
    expected_fingerprint: str,
    destination: Path,
) -> bool:
    if not isinstance(record, dict) or not destination.is_file():
        return False
    expected_size = record.get("outputSize")
    expected_mtime_ns = record.get("outputMtimeNs")
    return (
        record.get("fingerprint") == expected_fingerprint
        and record.get("output") == destination.name
        and isinstance(expected_size, int)
        and destination.stat().st_size == expected_size
        and isinstance(expected_mtime_ns, int)
        and destination.stat().st_mtime_ns == expected_mtime_ns
    )


def cached_audio_is_current(
    record: Any,
    *,
    expected_fingerprint: str,
    destination: Path,
    file_hash_cache: dict[tuple[str, int, int], str],
) -> bool:
    if not isinstance(record, dict) or not destination.is_file():
        return False
    expected_size = record.get("outputSize")
    expected_sha256 = record.get("outputSha256")
    if not (
        record.get("fingerprint") == expected_fingerprint
        and record.get("output") == destination.name
        and isinstance(expected_size, int)
        and destination.stat().st_size == expected_size
        and isinstance(expected_sha256, str)
    ):
        return False
    return file_sha256(destination, cache=file_hash_cache) == expected_sha256


def is_safe_owned_filename(value: str, *, suffix: str) -> bool:
    return bool(value) and value not in {".", ".."} and Path(value).name == value and value.endswith(suffix)


def collect_previous_owned_files(
    *,
    project_root: Path,
    previous_manifest: dict[str, Any] | None,
) -> dict[str, set[str]]:
    """Return only files that a previous Director build explicitly owned.

    A versioned manifest is authoritative once present. For projects being
    migrated from the legacy per-line layout, timeline references are the only
    ownership evidence. An unreadable/unsupported manifest deliberately
    disables the timeline fallback so a damaged manifest cannot broaden the
    cleanup boundary.
    """
    previous_owned: dict[str, set[str]] = {"images": set(), "audio": set()}
    if previous_manifest is not None:
        owned_files = previous_manifest.get("ownedFiles")
        if not isinstance(owned_files, dict):
            return previous_owned
        for kind, suffix in (("images", ".png"), ("audio", ".mp3")):
            values = owned_files.get(kind)
            if not isinstance(values, list):
                continue
            previous_owned[kind].update(
                value
                for value in values
                if isinstance(value, str) and is_safe_owned_filename(value, suffix=suffix)
            )
        return previous_owned

    manifest_path = project_root / DIRECTOR_MANIFEST_FILENAME
    if manifest_path.exists():
        return previous_owned

    timeline_path = project_root / "timeline.json"
    if not timeline_path.exists():
        return previous_owned
    try:
        timeline = read_json(timeline_path)
    except DirectorError as exc:
        print(f"warning: ignoring unreadable legacy timeline {timeline_path}: {exc}", file=sys.stderr)
        return previous_owned

    for collection, key, kind, suffix in (
        ("elements", "imageUrl", "images", ".png"),
        ("audio", "audioUrl", "audio", ".mp3"),
    ):
        entries = timeline.get(collection)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get(key), str):
                continue
            filename = f"{entry[key]}{suffix}"
            if is_safe_owned_filename(filename, suffix=suffix):
                previous_owned[kind].add(filename)
    return previous_owned


def prune_unreferenced_generated_files(
    *,
    image_dir: Path,
    audio_dir: Path,
    previous_owned: dict[str, set[str]],
    current_owned: dict[str, set[str]],
) -> None:
    """Prune stale files previously owned by Director; never infer ownership by scanning."""
    if image_dir.name != "images" or audio_dir.name != "audio" or image_dir.parent != audio_dir.parent:
        raise DirectorError("refusing to prune unexpected Director output directories")
    for kind, directory, suffix in (
        ("images", image_dir, ".png"),
        ("audio", audio_dir, ".mp3"),
    ):
        stale_names = previous_owned.get(kind, set()) - current_owned.get(kind, set())
        for name in sorted(stale_names):
            if not is_safe_owned_filename(name, suffix=suffix):
                continue
            candidate = directory / name
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise DirectorError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DirectorError(f"expected JSON object: {path}")
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == serialized:
                return
        except UnicodeDecodeError:
            pass
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise DirectorError(f"tool not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        raise DirectorError(f"command failed: {' '.join(command)}\n{stderr}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
