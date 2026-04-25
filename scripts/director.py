from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image


DEFAULT_BACKGROUND_CATALOG = Path("scripts") / "background_catalog.json"
DEFAULT_CHARACTERS_ROOT = Path("characters")
DEFAULT_CONTENT_ROOT = Path("my-video") / "public" / "content"
DEFAULT_CANVAS_SIZE = (1920, 1080)
DEFAULT_LINE_DURATION_MS = 3000


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
    build.add_argument("--overwrite", action="store_true", help="Overwrite generated images and copied audio.")

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

    catalog = read_json(Path(args.background_catalog))
    lines = flatten_lines(script)
    if not lines:
        raise DirectorError("script has no lines")
    canvas_size = (args.width, args.height)

    project_root = Path(args.content_root) / project
    image_dir = project_root / "images"
    audio_dir = project_root / "audio"
    image_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

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

    current_ms = 0
    for index, line in enumerate(lines, start=1):
        line_id = str(line.get("id") or f"l{index:03d}")
        audio_path = prepare_audio(
            line=line,
            destination=audio_dir / f"{line_id}.mp3",
            repo_root=repo_root,
            overwrite=args.overwrite,
        )
        duration_ms = audio_duration_ms(audio_path) if audio_path.exists() else estimated_duration_ms(line)
        image_path = image_dir / f"{line_id}.png"
        if image_path.exists() and not args.overwrite:
            raise DirectorError(f"image already exists, pass --overwrite: {image_path}")
        compose_frame(
            line=line,
            destination=image_path,
            catalog=catalog,
            characters_root=Path(args.characters_root),
            repo_root=repo_root,
            canvas_size=canvas_size,
        )

        start_ms = current_ms
        end_ms = current_ms + duration_ms
        timeline["elements"].append(
            {
                "startMs": start_ms,
                "endMs": end_ms,
                "imageUrl": line_id,
                "enterTransition": "none",
                "exitTransition": "none",
            }
        )
        if audio_path.exists():
            timeline["audio"].append({"startMs": start_ms, "endMs": end_ms, "audioUrl": line_id})

        subtitle = format_subtitle(line)
        if subtitle:
            timeline["text"].append(
                {
                    "startMs": start_ms,
                    "endMs": end_ms,
                    "text": subtitle,
                    "position": "center",
                }
            )

        descriptor["content"].append(
            {
                "uid": line_id,
                "text": str(line.get("spokenText") or subtitle or ""),
                "imageDescription": str(line.get("background") or ""),
                "durationMs": duration_ms,
            }
        )
        current_ms = end_ms

    write_json(project_root / "timeline.json", timeline)
    write_json(project_root / "descriptor.json", descriptor)
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
) -> None:
    background_path = resolve_background_path(line.get("background"), catalog, repo_root)
    canvas = crop_cover(Image.open(background_path).convert("RGBA"), canvas_size)

    for character in character_specs_for_line(line):
        sprite_path = resolve_sprite_path(line=character, characters_root=characters_root, repo_root=repo_root)
        sprite = Image.open(sprite_path).convert("RGBA")
        scale = float(character.get("spriteScale", 0.92))
        sprite = resize_sprite(sprite, target_height=int(canvas_size[1] * scale))
        x, y = character_position(character=character, sprite=sprite, canvas_size=canvas_size)
        canvas.alpha_composite(sprite, (x, y))

    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(destination, quality=95)


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
) -> Path:
    if destination.exists() and not overwrite:
        return destination

    audio_value = line.get("audio")
    if not audio_value:
        return destination

    source = resolve_existing_path(str(audio_value), repo_root=repo_root, label="audio")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".mp3":
        shutil.copy2(source, destination)
    else:
        run(["ffmpeg", "-y", "-i", str(source), "-codec:a", "libmp3lame", "-q:a", "2", str(destination)])
    return destination


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


def format_subtitle(line: dict[str, Any]) -> str:
    if line.get("subtitle"):
        subtitle = str(line["subtitle"]).strip()
        return prefix_subtitle(line=line, subtitle=subtitle)
    parts = [str(line.get("subtitleJa") or "").strip(), str(line.get("subtitleZh") or "").strip()]
    parts = [part for part in parts if part]
    if parts:
        return prefix_subtitle(line=line, subtitle="\n".join(parts))
    return prefix_subtitle(line=line, subtitle=str(line.get("spokenText") or "").strip())


def prefix_subtitle(*, line: dict[str, Any], subtitle: str) -> str:
    if not subtitle:
        return ""
    label = line.get("speakerLabel")
    if label is None:
        label = default_speaker_label(line)
    label_text = str(label or "").strip()
    if not label_text:
        return subtitle
    return f"{label_text}: {subtitle}"


def default_speaker_label(line: dict[str, Any]) -> str:
    if str(line.get("type") or "") == "narration":
        return "旁白"
    speaker_id = str(line.get("speakerId") or "")
    if speaker_id == "trainer":
        return "训练员"
    return ""


def resolve_existing_path(path_value: str, *, repo_root: Path, label: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    path = path.resolve()
    if not path.exists():
        raise DirectorError(f"{label} not found: {path}")
    return path


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
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


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
