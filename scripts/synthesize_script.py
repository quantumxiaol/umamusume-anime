from __future__ import annotations

import argparse
import json
import tempfile
import sys
from pathlib import Path
from typing import Any

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    # Manifest helpers import this module lazily; share the same exception/helpers
    # when invoked as a file instead of loading a second __main__ module instance.
    sys.modules["scripts.synthesize_script"] = sys.modules[__name__]

from scripts.tts_manifest import cache_status, record, request_fingerprint
from scripts.workflow_io import lines as validated_lines, write as write_json
from my_tts.config import get_tts_settings

from my_tts.cli import (
    CliError,
    DEFAULT_FISH_STYLE_SYNTAX,
    FishSpeechClient,
    Qwen3TTSClient,
    extract_fish_gen_kwargs,
    extract_qwen_gen_kwargs,
    format_fish_speech_text,
    materialize_fish_stored_audio,
    materialize_qwen_stored_audio,
    parse_bool,
    transcode_to_wav,
)


DEFAULT_CHARACTERS_ROOT = Path("characters")


class SynthesisError(RuntimeError):
    pass


class SynthesisParser(argparse.ArgumentParser):
    def parse_known_args(self, args=None, namespace=None):
        parsed, remaining = super().parse_known_args(args, namespace)
        settings = get_tts_settings()
        if parsed.tts_engine not in ("fishspeech", "qwen3tts"):
            self.error("TTS_PROVIDER must be fishspeech or qwen3tts")
        parsed.fish_tts_url = parsed.fish_tts_url or settings.url("fishspeech")
        parsed.qwen3tts_url = parsed.qwen3tts_url or settings.url("qwen3tts")
        parsed.model_id = parsed.model_id or settings.model(parsed.tts_engine)
        parsed.language = parsed.language or settings.language(parsed.tts_engine)
        return parsed, remaining


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        synthesize_script(args)
    except (CliError, SynthesisError, ValueError, OSError, httpx.HTTPError) as exc:
        print(f"error: {exc}")
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = SynthesisParser(description="Synthesize audio for structured script lines.")
    parser.add_argument("--script", required=True, help="Structured script JSON.")
    parser.add_argument("--characters-root", default=str(DEFAULT_CHARACTERS_ROOT), help="Character assets root.")
    parser.add_argument(
        "--tts-engine",
        choices=("qwen3tts", "fishspeech"),
        default=get_tts_settings().provider,
        required=get_tts_settings().provider is None,
        help="Voice clone backend (default: TTS_PROVIDER).",
    )
    parser.add_argument("--qwen3tts-url", help="Qwen3-TTS service URL (default: matching TTS_API_BASE_URL).")
    parser.add_argument("--fish-tts-url", help="Fish Speech service URL (default: matching TTS_API_BASE_URL).")
    parser.add_argument("--timeout", type=float, default=300.0, help="HTTP timeout in seconds.")
    parser.add_argument("--language", help="Language (default: TTS_LANGUAGE; Fish detects language from text).")
    parser.add_argument(
        "--speaker-id",
        action="append",
        help="Only synthesize lines for this speakerId. Can be passed more than once.",
    )
    parser.add_argument(
        "--line-id",
        action="append",
        help="Only synthesize lines with this id. Can be passed more than once.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Regenerate existing audio files.")
    parser.add_argument("--model-id", help="Model/checkpoint version (default: TTS_MODEL); records identity, does not load server weights.")
    parser.add_argument("--reuse-unverified", action="store_true", help="Explicitly keep legacy WAVs without receipts (never permits stale/modified WAVs).")
    parser.add_argument("--audit", action="store_true", help="Report cache status without contacting TTS or writing audio.")
    parser.add_argument("--audit-output", help="Optional JSON report for --audit.")
    parser.add_argument("--max-new-tokens", type=int)
    parser.add_argument(
        "--non-streaming-mode",
        nargs="?",
        const="true",
        default=None,
        type=parse_bool,
        help="Forward non_streaming_mode to Qwen3-TTS for full-text voice clone generation.",
    )
    parser.add_argument(
        "--do-sample",
        nargs="?",
        const="true",
        default=None,
        type=parse_bool,
        help="Enable sampling. Accepts true/false; if omitted but sampling params are provided, it is inferred true.",
    )
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--repetition-penalty", type=float)
    parser.add_argument("--chunk-length", type=int, help="Forward chunk_length to Fish Speech.")
    parser.add_argument("--seed", type=int, help="Forward seed to Fish Speech.")
    parser.add_argument(
        "--use-memory-cache",
        choices=("on", "off"),
        help="Forward use_memory_cache to Fish Speech.",
    )
    parser.add_argument("--fish-format", default="wav", help="Fish Speech output audio format.")
    parser.add_argument(
        "--fish-style",
        dest="style",
        help=(
            "Fish Speech style preset or inline tag for all lines. Presets include bright, cute, "
            "confident, energetic, excited, fast, joyful, satisfied, soft, and teasing."
        ),
    )
    parser.add_argument(
        "--fish-style-tag",
        dest="style_tag",
        help="Raw Fish Speech inline style tag, for example '[excited][pitch up]' or '(excited)'.",
    )
    parser.add_argument(
        "--fish-style-syntax",
        choices=("s1", "s2"),
        default=None,
        dest="style_syntax",
        help="Inline style syntax for --fish-style presets. Use s2 for S2-Pro and s1 for S1-mini.",
    )
    parser.add_argument(
        "--no-fish-speaker-tag",
        action="store_false",
        default=None,
        dest="speaker_tag",
        help="Do not auto-prefix Fish Speech target text with <|speaker:0|>.",
    )
    parser.add_argument(
        "--subtalker-do-sample",
        nargs="?",
        const="true",
        default=None,
        type=parse_bool,
        help="Forward subtalker_do_sample to Qwen3-TTS.",
    )
    parser.add_argument("--subtalker-top-k", type=int)
    parser.add_argument("--subtalker-top-p", type=float)
    parser.add_argument("--subtalker-temperature", type=float)
    parser.add_argument(
        "--no-batch",
        action="store_true",
        help="Use the legacy one-request-per-line voice_clone endpoint.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=6,
        help="Maximum lines per voice_clone_batch_file request per speaker. 0 means all lines.",
    )
    return parser


def synthesize_script(args: argparse.Namespace) -> None:
    script_path = Path(args.script).expanduser().resolve()
    script = read_json(script_path)
    lines = validated_lines(script)
    targets = [line for line in lines if line.get("audio")]
    if args.speaker_id:
        speaker_ids = set(args.speaker_id)
        if speaker_ids - {line.get("speakerId") for line in targets}:
            raise SynthesisError("Unknown or unvoiced --speaker-id")
        targets = [line for line in targets if str(line.get("speakerId") or "") in speaker_ids]
    if args.line_id:
        line_ids = set(args.line_id)
        if line_ids - {line.get("id") for line in lines if line.get("audio")}:
            raise SynthesisError("Unknown or unvoiced --line-id")
        targets = [line for line in targets if str(line.get("id") or "") in line_ids]
    if not targets:
        print("no audio targets found")
        return

    pending: list[dict[str, Any]] = []
    audit = []
    blocked = []
    args._requests = {}
    for index, line in enumerate(targets, start=1):
        line_id = str(line.get("id") or f"line_{index:03d}")
        output_path = Path(str(line["audio"])).expanduser()
        request = request_fingerprint(line, args)
        args._requests[line_id] = request
        status = cache_status(output_path, request)
        audit.append({"id": line_id, "speakerId": line["speakerId"], "audio": str(output_path), "status": status})
        if args.audit:
            print(f"{line_id}: {status}")
        elif output_path.exists() and not args.overwrite:
            if status in ("fresh", "accepted") or (status == "unverified" and args.reuse_unverified):
                print(f"[{index}/{len(targets)}] keep {status} {line_id}: {output_path}", flush=True)
            else:
                blocked.append(f"{line_id}={status}")
        else:
            pending.append(line)
    if args.audit:
        if args.audit_output:
            write_json(Path(args.audit_output), {"lines": audit})
        return
    if blocked:
        raise SynthesisError("Audio requires review: " + ", ".join(blocked) +
                             ". Use video_workflow repair, or explicitly --overwrite selected lines. "
                             "Legacy only: --reuse-unverified preserves without claiming verification.")
    if not pending:
        print("all selected audio targets retained")
        return

    get_tts_settings().require_enabled()
    if args.tts_engine == "fishspeech":
        client: Qwen3TTSClient | FishSpeechClient = FishSpeechClient(base_url=args.fish_tts_url, timeout=args.timeout)
    else:
        client = Qwen3TTSClient(base_url=args.qwen3tts_url, timeout=args.timeout)
    try:
        health = client.health()
        if health.get("status") != "ok":
            raise SynthesisError(f"TTS service is not healthy: {health.get('status')}")
        with tempfile.TemporaryDirectory(prefix="script-tts-") as temp_name:
            temp_dir = Path(temp_name)
            if args.no_batch:
                for index, line in enumerate(pending, start=1):
                    synthesize_line(
                        client=client,
                        line=line,
                        index=index,
                        total=len(pending),
                        characters_root=Path(args.characters_root),
                        temp_dir=temp_dir,
                        args=args,
                    )
            else:
                synthesize_batches(
                    client=client,
                    targets=pending,
                    characters_root=Path(args.characters_root),
                    temp_dir=temp_dir,
                    args=args,
                )
    finally:
        client.close()


def synthesize_batches(
    *,
    client: Qwen3TTSClient | FishSpeechClient,
    targets: list[dict[str, Any]],
    characters_root: Path,
    temp_dir: Path,
    args: argparse.Namespace,
) -> None:
    groups: dict[str, list[dict[str, Any]]] = {}
    for line in targets:
        line_id = str(line.get("id") or "")
        speaker_id = str(line.get("speakerId") or "")
        if not speaker_id:
            raise SynthesisError(f"line {line_id} has audio but no speakerId")
        groups.setdefault(speaker_id, []).append(line)

    completed = 0
    total = len(targets)
    for speaker_id, lines in groups.items():
        character_dir = resolve_character_dir(characters_root=characters_root, speaker_id=speaker_id)
        reference_audio = resolve_reference_audio(character_dir=character_dir, speaker_id=speaker_id)
        reference_text = resolve_existing(
            character_dir / "reference_jp.txt",
            label=f"{speaker_id} reference text",
        ).read_text(encoding="utf-8").strip()
        if not reference_text:
            raise SynthesisError(f"empty reference text: {character_dir / 'reference_jp.txt'}")

        request_reference = reference_audio
        if args.tts_engine == "qwen3tts":
            normalized_reference = temp_dir / f"{safe_filename(speaker_id)}_reference.wav"
            if not normalized_reference.exists():
                transcode_to_wav(reference_audio, normalized_reference)
            request_reference = normalized_reference

        for batch_index, batch in enumerate(chunk_lines(lines, args.batch_size), start=1):
            batch_texts = [
                fish_line_spoken_text(line, args=args)
                if args.tts_engine == "fishspeech"
                else line_spoken_text(line)
                for line in batch
            ]
            text_file = temp_dir / f"{safe_filename(speaker_id)}_batch_{batch_index:03d}.txt"
            text_file.write_text("\n".join(batch_texts) + "\n", encoding="utf-8")

            first = completed + 1
            last = completed + len(batch)
            print(
                f"[{first}-{last}/{total}] batch speaker={speaker_id} lines={len(batch)}",
                flush=True,
            )
            if args.tts_engine == "fishspeech":
                assert isinstance(client, FishSpeechClient)
                payload = client.voice_clone_batch_file(
                    ref_audio_path=request_reference,
                    text_file=text_file,
                    ref_text=reference_text,
                    output_prefix=f"{safe_filename(speaker_id)}_{batch_index:03d}",
                    audio_format=args.fish_format,
                    **extract_fish_gen_kwargs(args),
                )
            else:
                assert isinstance(client, Qwen3TTSClient)
                payload = client.voice_clone_batch_file(
                    ref_audio_path=request_reference,
                    text_file=text_file,
                    ref_text=reference_text,
                    language=args.language,
                    output_prefix=f"{safe_filename(speaker_id)}_{batch_index:03d}",
                    **extract_qwen_gen_kwargs(args),
                )
            stored_files = expect_audio_paths(payload)
            if len(stored_files) != len(batch):
                raise SynthesisError(
                    f"{args.tts_engine} batch returned {len(stored_files)} files for {len(batch)} {speaker_id} lines"
                )

            for line, stored in zip(batch, stored_files, strict=True):
                line_id = str(line.get("id") or f"line_{completed + 1:03d}")
                output_path = Path(str(line["audio"])).expanduser()
                if args.tts_engine == "fishspeech":
                    assert isinstance(client, FishSpeechClient)
                    materialize_fish_stored_audio(
                        client=client,
                        stored=stored,
                        destination=output_path.resolve(),
                    )
                else:
                    assert isinstance(client, Qwen3TTSClient)
                    materialize_qwen_stored_audio(
                        client=client,
                        stored=stored,
                        destination=output_path.resolve(),
                    )
                completed += 1
                record_generated(line, args)
                print(
                    f"[{completed}/{total}] {line_id} speaker={speaker_id} batch-output={stored.get('filename', 'audio.wav')}",
                    flush=True,
                )


def synthesize_line(
    *,
    client: Qwen3TTSClient | FishSpeechClient,
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
    character_dir = resolve_character_dir(characters_root=characters_root, speaker_id=speaker_id)
    reference_audio = resolve_reference_audio(character_dir=character_dir, speaker_id=speaker_id)
    reference_text = resolve_existing(character_dir / "reference_jp.txt", label=f"{speaker_id} reference text").read_text(
        encoding="utf-8"
    ).strip()
    if not reference_text:
        raise SynthesisError(f"empty reference text: {character_dir / 'reference_jp.txt'}")

    text = fish_line_spoken_text(line, args=args) if args.tts_engine == "fishspeech" else line_spoken_text(line)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    request_reference = reference_audio
    if args.tts_engine == "qwen3tts":
        normalized_reference = temp_dir / f"{speaker_id}_reference.wav"
        if not normalized_reference.exists():
            transcode_to_wav(reference_audio, normalized_reference)
        request_reference = normalized_reference

    print(f"[{index}/{total}] {line_id} speaker={speaker_id}", flush=True)
    if args.tts_engine == "fishspeech":
        assert isinstance(client, FishSpeechClient)
        payload = client.voice_clone(
            ref_audio_path=request_reference,
            text=text,
            ref_text=reference_text,
            output_name=f"{line_id}.wav",
            audio_format=args.fish_format,
            **extract_fish_gen_kwargs(args),
        )
    else:
        assert isinstance(client, Qwen3TTSClient)
        payload = client.voice_clone(
            ref_audio_path=request_reference,
            text=text,
            ref_text=reference_text,
            language=args.language,
            output_name=f"{line_id}.wav",
            **extract_qwen_gen_kwargs(args),
        )
    stored = payload.get("audio")
    if not isinstance(stored, dict):
        raise SynthesisError(f"{args.tts_engine} response did not include audio for {line_id}")
    if args.tts_engine == "fishspeech":
        assert isinstance(client, FishSpeechClient)
        materialize_fish_stored_audio(client=client, stored=stored, destination=output_path.resolve())
    else:
        assert isinstance(client, Qwen3TTSClient)
        materialize_qwen_stored_audio(client=client, stored=stored, destination=output_path.resolve())
    record_generated(line, args)


def record_generated(line: dict[str, Any], args: argparse.Namespace) -> None:
    request = request_fingerprint(line, args)
    if request != args._requests[line["id"]]:
        raise SynthesisError(f"Reference/settings changed during generation: {line['id']}; output is unverified")
    endpoint = args.fish_tts_url if args.tts_engine == "fishspeech" else args.qwen3tts_url
    settings = {key: value for key, value in vars(args).items() if not key.startswith("_")}
    record(Path(line["audio"]).resolve(), line, request, endpoint=endpoint, settings=settings)


def chunk_lines(lines: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    if batch_size <= 0:
        return [lines]
    return [lines[index : index + batch_size] for index in range(0, len(lines), batch_size)]


def expect_audio_paths(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stored = payload.get("audio_paths")
    if not isinstance(stored, list) or not all(isinstance(item, dict) for item in stored):
        raise SynthesisError("batch response did not include expected audio_paths list")
    return stored


def line_spoken_text(line: dict[str, Any]) -> str:
    line_id = str(line.get("id") or "")
    text = str(line.get("spokenText") or "").strip()
    if not text:
        raise SynthesisError(f"line {line_id} has audio but no spokenText")
    return " ".join(text.splitlines())


def fish_line_spoken_text(line: dict[str, Any], *, args: argparse.Namespace) -> str:
    style = first_line_text(
        line,
        "fishStyle",
        "fishEmotion",
        "ttsStyle",
        "ttsEmotion",
        default=getattr(args, "style", None),
    )
    style_tag = first_line_text(
        line,
        "fishStyleTag",
        "fishEmotionTag",
        "ttsStyleTag",
        "ttsEmotionTag",
        default=getattr(args, "style_tag", None),
    )
    style_syntax = (
        first_line_text(
            line,
            "fishStyleSyntax",
            "ttsStyleSyntax",
            default=getattr(args, "style_syntax", None),
        )
        or DEFAULT_FISH_STYLE_SYNTAX
    )
    speaker_tag_default = getattr(args, "speaker_tag", None)
    speaker_tag = line_bool(
        line,
        "fishSpeakerTag",
        "ttsSpeakerTag",
        default=True if speaker_tag_default is None else speaker_tag_default,
    )
    return format_fish_speech_text(
        line_spoken_text(line),
        style=style,
        style_tag=style_tag,
        style_syntax=style_syntax,
        speaker_tag=speaker_tag,
    )


def first_line_text(line: dict[str, Any], *keys: str, default: str | None = None) -> str | None:
    for key in keys:
        value = line.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def line_bool(line: dict[str, Any], *keys: str, default: bool) -> bool:
    for key in keys:
        if key not in line:
            continue
        value = line[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                return parse_bool(value)
            except argparse.ArgumentTypeError as exc:
                line_id = str(line.get("id") or "")
                raise SynthesisError(f"line {line_id} has invalid {key}: {value!r}") from exc
        line_id = str(line.get("id") or "")
        raise SynthesisError(f"line {line_id} has invalid {key}: {value!r}")
    return default


def safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value) or "speaker"


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
