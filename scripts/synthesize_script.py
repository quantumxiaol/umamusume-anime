from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from my_tts.cli import (
    Qwen3TTSClient,
    extract_qwen_gen_kwargs,
    materialize_qwen_stored_audio,
    transcode_to_wav,
)


DEFAULT_CHARACTERS_ROOT = Path("characters")
DEFAULT_QWEN3TTS_URL = "http://127.0.0.1:8001"


class SynthesisError(RuntimeError):
    pass


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        synthesize_script(args)
    except SynthesisError as exc:
        print(f"error: {exc}")
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synthesize audio for structured script lines.")
    parser.add_argument("--script", required=True, help="Structured script JSON.")
    parser.add_argument("--characters-root", default=str(DEFAULT_CHARACTERS_ROOT), help="Character assets root.")
    parser.add_argument("--qwen3tts-url", default=DEFAULT_QWEN3TTS_URL, help="Qwen3-TTS service URL.")
    parser.add_argument("--timeout", type=float, default=300.0, help="HTTP timeout in seconds.")
    parser.add_argument("--language", default="Japanese", help="Qwen3-TTS language value.")
    parser.add_argument(
        "--speaker-id",
        action="append",
        help="Only synthesize lines for this speakerId. Can be passed more than once.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Regenerate existing audio files.")
    parser.add_argument("--max-new-tokens", type=int)
    parser.add_argument("--do-sample", action="store_true")
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--repetition-penalty", type=float)
    parser.add_argument("--subtalker-top-k", type=int)
    parser.add_argument("--subtalker-top-p", type=float)
    parser.add_argument("--subtalker-temperature", type=float)
    return parser


def synthesize_script(args: argparse.Namespace) -> None:
    script_path = Path(args.script).expanduser().resolve()
    script = read_json(script_path)
    lines = flatten_lines(script)
    targets = [line for line in lines if line.get("audio")]
    if args.speaker_id:
        speaker_ids = set(args.speaker_id)
        targets = [line for line in targets if str(line.get("speakerId") or "") in speaker_ids]
    if not targets:
        print("no audio targets found")
        return

    client = Qwen3TTSClient(base_url=args.qwen3tts_url, timeout=args.timeout)
    try:
        with tempfile.TemporaryDirectory(prefix="script-tts-") as temp_name:
            temp_dir = Path(temp_name)
            for index, line in enumerate(targets, start=1):
                synthesize_line(
                    client=client,
                    line=line,
                    index=index,
                    total=len(targets),
                    characters_root=Path(args.characters_root),
                    temp_dir=temp_dir,
                    args=args,
                )
    finally:
        client.close()


def synthesize_line(
    *,
    client: Qwen3TTSClient,
    line: dict[str, Any],
    index: int,
    total: int,
    characters_root: Path,
    temp_dir: Path,
    args: argparse.Namespace,
) -> None:
    line_id = str(line.get("id") or f"line_{index:03d}")
    speaker_id = str(line.get("speakerId") or "")
    if not speaker_id:
        raise SynthesisError(f"line {line_id} has audio but no speakerId")

    output_path = Path(str(line["audio"])).expanduser()
    if output_path.exists() and not args.overwrite:
        print(f"[{index}/{total}] skip existing {line_id}: {output_path}", flush=True)
        return

    character_dir = resolve_character_dir(characters_root=characters_root, speaker_id=speaker_id)
    reference_audio = resolve_reference_audio(character_dir=character_dir, speaker_id=speaker_id)
    reference_text = resolve_existing(character_dir / "reference_jp.txt", label=f"{speaker_id} reference text").read_text(
        encoding="utf-8"
    ).strip()
    if not reference_text:
        raise SynthesisError(f"empty reference text: {character_dir / 'reference_jp.txt'}")

    text = str(line.get("spokenText") or "").strip()
    if not text:
        raise SynthesisError(f"line {line_id} has audio but no spokenText")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_reference = temp_dir / f"{speaker_id}_reference.wav"
    if not normalized_reference.exists():
        transcode_to_wav(reference_audio, normalized_reference)

    print(f"[{index}/{total}] {line_id} speaker={speaker_id}", flush=True)
    payload = client.voice_clone(
        ref_audio_path=normalized_reference,
        text=text,
        ref_text=reference_text,
        language=args.language,
        output_name=f"{line_id}.wav",
        **extract_qwen_gen_kwargs(args),
    )
    stored = payload.get("audio")
    if not isinstance(stored, dict):
        raise SynthesisError(f"qwen response did not include audio for {line_id}")
    materialize_qwen_stored_audio(client=client, stored=stored, destination=output_path.resolve())


def resolve_character_dir(*, characters_root: Path, speaker_id: str) -> Path:
    normalized = speaker_id.removeprefix("uma_")
    character_dir = characters_root / normalized
    if not character_dir.exists():
        raise SynthesisError(f"character directory not found: {character_dir}")
    return character_dir


def resolve_reference_audio(*, character_dir: Path, speaker_id: str) -> Path:
    for filename in ("reference.mp3", "reference.wav"):
        path = character_dir / filename
        if path.exists():
            return path
    raise SynthesisError(f"{speaker_id} reference audio not found: {character_dir / 'reference.mp3'} or reference.wav")


def resolve_existing(path: Path, *, label: str) -> Path:
    if not path.exists():
        raise SynthesisError(f"{label} not found: {path}")
    return path


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SynthesisError(f"expected JSON object: {path}")
    return data


def flatten_lines(script: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(script.get("lines"), list):
        return [dict(line) for line in script["lines"] if isinstance(line, dict)]

    lines: list[dict[str, Any]] = []
    for scene in script.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        for line in scene.get("lines", []):
            if isinstance(line, dict):
                lines.append(dict(line))
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
