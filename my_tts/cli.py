from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import httpx

QWEN_API_PREFIX = "/qwen3tts"
DEFAULT_CONTENT_ROOT = Path("my-video") / "public" / "content"
DEFAULT_INDEXTTS_URL = "http://127.0.0.1:8000"
DEFAULT_QWEN3TTS_URL = "http://127.0.0.1:8001"
DEFAULT_REFERENCE_DIR_NAME = "reference-audio"


class CliError(RuntimeError):
    """Expected CLI error with a concise message."""


@dataclass(slots=True)
class ProjectPaths:
    slug: str
    root: Path
    descriptor_path: Path
    timeline_path: Path
    audio_dir: Path
    image_dir: Path
    reference_audio_dir: Path


class IndexTTSClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def synthesize(
        self,
        *,
        text: str,
        prompt_wav_path: str,
        output_name: str,
        emo_audio_prompt: str | None = None,
        emo_alpha: float = 1.0,
        emo_text: str | None = None,
        use_emo_text: bool = False,
        use_random: bool = False,
        interval_silence: int = 200,
        max_text_tokens_per_segment: int = 120,
        verbose: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "text": text,
            "prompt_wav_path": prompt_wav_path,
            "output_name": output_name,
            "emo_audio_prompt": emo_audio_prompt,
            "emo_alpha": emo_alpha,
            "emo_vector": None,
            "use_emo_text": use_emo_text or bool(emo_text),
            "emo_text": emo_text,
            "use_random": use_random,
            "interval_silence": interval_silence,
            "max_text_tokens_per_segment": max_text_tokens_per_segment,
            "verbose": verbose,
        }
        response = self._client.post("/tts/synthesize", json=payload)
        response.raise_for_status()
        return response.json()


class Qwen3TTSClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        normalized = base_url.rstrip("/")
        if normalized.endswith(QWEN_API_PREFIX):
            normalized = normalized[: -len(QWEN_API_PREFIX)]
        self._client = httpx.Client(base_url=normalized, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        return response.json()

    def download(self, url: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._client.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        return destination

    def health(self) -> dict[str, Any]:
        return self._json(self._client.get(f"{QWEN_API_PREFIX}/health"))

    def list_narrators(self) -> dict[str, Any]:
        return self._json(self._client.get(f"{QWEN_API_PREFIX}/tts/narrators"))

    def voice_clone(
        self,
        *,
        ref_audio_path: Path,
        text: str | None = None,
        text_file: Path | None = None,
        ref_text: str | None = None,
        ref_text_file: Path | None = None,
        language: str = "Auto",
        output_name: str | None = None,
        x_vector_only_mode: bool = False,
        **gen_kwargs: Any,
    ) -> dict[str, Any]:
        with ExitStack() as stack:
            data: dict[str, str] = {
                "language": language,
                "x_vector_only_mode": _form_value(x_vector_only_mode),
            }
            files: dict[str, tuple[str, Any, str]] = {
                "ref_audio": (
                    ref_audio_path.name,
                    stack.enter_context(ref_audio_path.open("rb")),
                    "audio/wav",
                )
            }

            if text is not None:
                data["text"] = text
            if ref_text is not None:
                data["ref_text"] = ref_text
            if output_name is not None:
                data["output_name"] = output_name
            if text_file is not None:
                files["text_file"] = (
                    text_file.name,
                    stack.enter_context(text_file.open("rb")),
                    "text/plain",
                )
            if ref_text_file is not None:
                files["ref_text_file"] = (
                    ref_text_file.name,
                    stack.enter_context(ref_text_file.open("rb")),
                    "text/plain",
                )
            for key, value in gen_kwargs.items():
                if value is not None:
                    data[key] = _form_value(value)

            response = self._client.post(f"{QWEN_API_PREFIX}/tts/voice_clone", data=data, files=files)
        return self._json(response)

    def voice_clone_batch_file(
        self,
        *,
        ref_audio_path: Path,
        text_file: Path | None = None,
        texts: Sequence[str] | None = None,
        ref_text: str | None = None,
        ref_text_file: Path | None = None,
        language: str = "Auto",
        output_prefix: str | None = None,
        x_vector_only_mode: bool = False,
        **gen_kwargs: Any,
    ) -> dict[str, Any]:
        if text_file is not None and texts:
            raise ValueError("Provide either text_file or texts, not both.")
        if text_file is None and not texts:
            raise ValueError("Either text_file or texts must be provided.")

        with ExitStack() as stack:
            data: dict[str, Any] = {
                "language": language,
                "x_vector_only_mode": _form_value(x_vector_only_mode),
            }
            files: dict[str, tuple[str, Any, str]] = {
                "ref_audio": (
                    ref_audio_path.name,
                    stack.enter_context(ref_audio_path.open("rb")),
                    "audio/wav",
                ),
            }
            if text_file is not None:
                files["text_file"] = (
                    text_file.name,
                    stack.enter_context(text_file.open("rb")),
                    "text/plain",
                )
            if texts:
                data["text"] = list(texts)
            if ref_text is not None:
                data["ref_text"] = ref_text
            if output_prefix is not None:
                data["output_prefix"] = output_prefix
            if ref_text_file is not None:
                files["ref_text_file"] = (
                    ref_text_file.name,
                    stack.enter_context(ref_text_file.open("rb")),
                    "text/plain",
                )
            for key, value in gen_kwargs.items():
                if value is not None:
                    data[key] = _form_value(value)

            response = self._client.post(f"{QWEN_API_PREFIX}/tts/voice_clone_batch_file", data=data, files=files)
        return self._json(response)

    def narration(
        self,
        *,
        text: str | None = None,
        text_file: Path | None = None,
        language: str = "Auto",
        speaker: str | None = None,
        instruct: str | None = None,
        output_name: str | None = None,
        **gen_kwargs: Any,
    ) -> dict[str, Any]:
        with ExitStack() as stack:
            data: dict[str, str] = {"language": language}
            files: dict[str, tuple[str, Any, str]] = {}

            if text is not None:
                data["text"] = text
            if speaker is not None:
                data["speaker"] = speaker
            if instruct is not None:
                data["instruct"] = instruct
            if output_name is not None:
                data["output_name"] = output_name
            if text_file is not None:
                files["text_file"] = (
                    text_file.name,
                    stack.enter_context(text_file.open("rb")),
                    "text/plain",
                )
            for key, value in gen_kwargs.items():
                if value is not None:
                    data[key] = _form_value(value)

            response = self._client.post(f"{QWEN_API_PREFIX}/tts/narration", data=data, files=files)
        return self._json(response)

    def narration_batch_file(
        self,
        *,
        text_file: Path,
        language: str = "Auto",
        speaker: str | None = None,
        instruct: str | None = None,
        output_prefix: str | None = None,
        **gen_kwargs: Any,
    ) -> dict[str, Any]:
        with ExitStack() as stack:
            data: dict[str, str] = {"language": language}
            files: dict[str, tuple[str, Any, str]] = {
                "text_file": (
                    text_file.name,
                    stack.enter_context(text_file.open("rb")),
                    "text/plain",
                )
            }
            if speaker is not None:
                data["speaker"] = speaker
            if instruct is not None:
                data["instruct"] = instruct
            if output_prefix is not None:
                data["output_prefix"] = output_prefix
            for key, value in gen_kwargs.items():
                if value is not None:
                    data[key] = _form_value(value)

            response = self._client.post(f"{QWEN_API_PREFIX}/tts/narration_batch_file", data=data, files=files)
        return self._json(response)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="my-tts",
        description="Project-level and service-level CLI for local IndexTTS and Qwen3TTS backends.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_projects = subparsers.add_parser("list-projects", help="List content projects under my-video/public/content.")
    list_projects.add_argument("--content-root", default=str(DEFAULT_CONTENT_ROOT), help="Content root directory.")

    clone = subparsers.add_parser("clone-project", help="Clone voice for every content item in a project.")
    add_clone_project_args(clone)

    qwen = subparsers.add_parser("qwen", help="Call Qwen3TTS service endpoints directly.")
    qwen_subparsers = qwen.add_subparsers(dest="qwen_command", required=True)

    qwen_health = qwen_subparsers.add_parser("health", help="Call GET /qwen3tts/health.")
    add_qwen_common_args(qwen_health)

    qwen_narrators = qwen_subparsers.add_parser("list-narrators", help="Call GET /qwen3tts/tts/narrators.")
    add_qwen_common_args(qwen_narrators)

    qwen_voice_clone = qwen_subparsers.add_parser("voice-clone", help="Call POST /qwen3tts/tts/voice_clone.")
    add_qwen_common_args(qwen_voice_clone)
    add_qwen_generation_args(qwen_voice_clone)
    qwen_voice_clone.add_argument("--ref-audio", required=True, help="Reference audio path.")
    qwen_voice_clone.add_argument("--text", help="Inline synthesis text.")
    qwen_voice_clone.add_argument("--text-file", help="Synthesis text file.")
    qwen_voice_clone.add_argument("--ref-text", help="Inline reference transcript.")
    qwen_voice_clone.add_argument("--ref-text-file", help="Reference transcript file.")
    qwen_voice_clone.add_argument("--language", default="Auto", help="Qwen language form value.")
    qwen_voice_clone.add_argument("--output-name", help="Optional output filename on the server.")
    qwen_voice_clone.add_argument("--x-vector-only-mode", action="store_true", help="Enable x_vector_only_mode.")
    qwen_voice_clone.add_argument("--download-to", help="Optional local file path for the returned audio.")

    qwen_voice_clone_batch = qwen_subparsers.add_parser(
        "voice-clone-batch-file",
        aliases=["voice-clone-batch"],
        help="Call POST /qwen3tts/tts/voice_clone_batch_file.",
    )
    add_qwen_common_args(qwen_voice_clone_batch)
    add_qwen_generation_args(qwen_voice_clone_batch)
    qwen_voice_clone_batch.add_argument("--ref-audio", required=True, help="Reference audio path.")
    qwen_voice_clone_batch.add_argument("--text", action="append", help="Inline synthesis text. Can be repeated.")
    qwen_voice_clone_batch.add_argument("--text-file", help="Input text file, one line per sample.")
    qwen_voice_clone_batch.add_argument("--ref-text", help="Inline reference transcript for the prompt audio.")
    qwen_voice_clone_batch.add_argument("--ref-text-file", help="Reference transcript file for the prompt audio.")
    qwen_voice_clone_batch.add_argument("--language", default="Auto", help="Qwen language form value.")
    qwen_voice_clone_batch.add_argument("--output-prefix", help="Output filename prefix on the server.")
    qwen_voice_clone_batch.add_argument("--x-vector-only-mode", action="store_true", help="Enable x_vector_only_mode.")
    qwen_voice_clone_batch.add_argument("--download-dir", help="Optional local directory for returned audios.")

    qwen_narration = qwen_subparsers.add_parser("narration", help="Call POST /qwen3tts/tts/narration.")
    add_qwen_common_args(qwen_narration)
    add_qwen_generation_args(qwen_narration)
    qwen_narration.add_argument("--text", help="Inline narration text.")
    qwen_narration.add_argument("--text-file", help="Narration text file.")
    qwen_narration.add_argument("--language", default="Auto", help="Narration language.")
    qwen_narration.add_argument("--speaker", help="Narration speaker.")
    qwen_narration.add_argument("--instruct", help="Narration style instruction.")
    qwen_narration.add_argument("--output-name", help="Optional output filename on the server.")
    qwen_narration.add_argument("--download-to", help="Optional local file path for the returned audio.")

    qwen_narration_batch = qwen_subparsers.add_parser(
        "narration-batch-file",
        help="Call POST /qwen3tts/tts/narration_batch_file.",
    )
    add_qwen_common_args(qwen_narration_batch)
    add_qwen_generation_args(qwen_narration_batch)
    qwen_narration_batch.add_argument("--text-file", required=True, help="Input text file, one line per sample.")
    qwen_narration_batch.add_argument("--language", default="Auto", help="Narration language.")
    qwen_narration_batch.add_argument("--speaker", help="Narration speaker.")
    qwen_narration_batch.add_argument("--instruct", help="Narration style instruction.")
    qwen_narration_batch.add_argument("--output-prefix", help="Output filename prefix on the server.")
    qwen_narration_batch.add_argument("--download-dir", help="Optional local directory for returned audios.")

    return parser


def add_clone_project_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("project", help="Project slug under the content root.")
    parser.add_argument("--engine", choices=("indextts", "qwen3tts"), required=True, help="Which backend service to use.")
    parser.add_argument("--content-root", default=str(DEFAULT_CONTENT_ROOT), help="Content root directory.")
    parser.add_argument(
        "--script-file",
        help="Optional replacement text file. Supports .txt line-per-item, JSON array, or JSON uid->text mapping.",
    )
    parser.add_argument("--text-key", default="text", help="Descriptor field to use when --script-file is omitted.")
    parser.add_argument(
        "--reference-script-file",
        help="Optional reference transcript file for qwen3tts. Defaults to the original descriptor text.",
    )
    parser.add_argument(
        "--shared-reference-audio",
        help="Use one reference audio file for every item instead of the project's existing audio/<uid>.mp3.",
    )
    parser.add_argument(
        "--reference-dir-name",
        default=DEFAULT_REFERENCE_DIR_NAME,
        help="Backup directory name used to preserve original per-item reference audio.",
    )
    parser.add_argument(
        "--no-backup-reference-audio",
        action="store_true",
        help="Disable automatic backup of current audio files before they are replaced.",
    )
    parser.add_argument("--timeout", type=float, default=300.0, help="HTTP timeout in seconds.")
    parser.add_argument("--indextts-url", default=DEFAULT_INDEXTTS_URL, help="IndexTTS service base URL.")
    parser.add_argument("--qwen3tts-url", default=DEFAULT_QWEN3TTS_URL, help="Qwen3TTS service base URL.")
    parser.add_argument("--qwen-language", default="Japanese", help="Qwen3TTS language form value.")
    parser.add_argument(
        "--qwen-x-vector-only-mode",
        action="store_true",
        help="Enable x_vector_only_mode for qwen3tts and skip ref_text validation.",
    )
    add_qwen_generation_args(parser, prefix="qwen-")
    parser.add_argument("--index-emo-audio-prompt", help="Optional emotion reference audio for IndexTTS.")
    parser.add_argument("--index-emo-alpha", type=float, default=1.0, help="Emotion reference weight for IndexTTS.")
    parser.add_argument("--index-emo-text", help="Optional emotion text for IndexTTS.")
    parser.add_argument("--index-use-random", action="store_true", help="Enable stochastic generation on IndexTTS.")
    parser.add_argument("--index-verbose", action="store_true", help="Enable verbose generation on IndexTTS.")
    parser.add_argument(
        "--index-max-text-tokens-per-segment",
        type=int,
        default=120,
        help="Forwarded to IndexTTS.",
    )
    parser.add_argument(
        "--index-interval-silence",
        type=int,
        default=200,
        help="Forwarded to IndexTTS in milliseconds.",
    )


def add_qwen_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=DEFAULT_QWEN3TTS_URL, help="Qwen3TTS service base URL.")
    parser.add_argument("--timeout", type=float, default=300.0, help="HTTP timeout in seconds.")


def add_qwen_generation_args(parser: argparse.ArgumentParser, prefix: str = "") -> None:
    parser.add_argument(f"--{prefix}max-new-tokens", type=int, dest=f"{prefix_to_dest(prefix)}max_new_tokens")
    parser.add_argument(
        f"--{prefix}do-sample",
        nargs="?",
        const="true",
        default=None,
        type=parse_bool,
        dest=f"{prefix_to_dest(prefix)}do_sample",
    )
    parser.add_argument(f"--{prefix}top-k", type=int, dest=f"{prefix_to_dest(prefix)}top_k")
    parser.add_argument(f"--{prefix}top-p", type=float, dest=f"{prefix_to_dest(prefix)}top_p")
    parser.add_argument(f"--{prefix}temperature", type=float, dest=f"{prefix_to_dest(prefix)}temperature")
    parser.add_argument(f"--{prefix}repetition-penalty", type=float, dest=f"{prefix_to_dest(prefix)}repetition_penalty")
    parser.add_argument(f"--{prefix}subtalker-top-k", type=int, dest=f"{prefix_to_dest(prefix)}subtalker_top_k")
    parser.add_argument(f"--{prefix}subtalker-top-p", type=float, dest=f"{prefix_to_dest(prefix)}subtalker_top_p")
    parser.add_argument(
        f"--{prefix}subtalker-temperature",
        type=float,
        dest=f"{prefix_to_dest(prefix)}subtalker_temperature",
    )


def prefix_to_dest(prefix: str) -> str:
    return prefix.replace("-", "_")


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected boolean value, got {value!r}")


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "list-projects":
            return cmd_list_projects(args)
        if args.command == "clone-project":
            return cmd_clone_project(args)
        if args.command == "qwen":
            return cmd_qwen(args)
        parser.error(f"Unsupported command: {args.command}")
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text.strip()
        message = detail or str(exc)
        print(f"http error: {message}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"http error: {exc}", file=sys.stderr)
        return 1

    return 0


def cmd_qwen(args: argparse.Namespace) -> int:
    client = Qwen3TTSClient(base_url=args.base_url, timeout=args.timeout)
    try:
        if args.qwen_command == "health":
            emit_json(client.health())
            return 0

        if args.qwen_command == "list-narrators":
            emit_json(client.list_narrators())
            return 0

        if args.qwen_command == "voice-clone":
            validate_text_inputs(args.text, args.text_file, field_name="text")
            validate_reference_text_inputs(
                ref_text=args.ref_text,
                ref_text_file=args.ref_text_file,
                x_vector_only_mode=args.x_vector_only_mode,
            )
            payload = client.voice_clone(
                ref_audio_path=resolve_existing_file(args.ref_audio, label="ref audio"),
                text=args.text,
                text_file=resolve_optional_file(args.text_file),
                ref_text=args.ref_text,
                ref_text_file=resolve_optional_file(args.ref_text_file),
                language=args.language,
                output_name=args.output_name,
                x_vector_only_mode=args.x_vector_only_mode,
                **extract_qwen_gen_kwargs(args),
            )
            downloaded = None
            if args.download_to:
                downloaded = download_qwen_audio_file(
                    client=client,
                    stored=expect_stored_file(payload, "audio"),
                    destination=Path(args.download_to).expanduser().resolve(),
                )
            emit_json(with_downloaded_field(payload, downloaded))
            return 0

        if args.qwen_command in {"voice-clone-batch-file", "voice-clone-batch"}:
            validate_text_inputs(args.text, args.text_file, field_name="text")
            validate_reference_text_inputs(
                ref_text=args.ref_text,
                ref_text_file=args.ref_text_file,
                x_vector_only_mode=args.x_vector_only_mode,
            )
            payload = client.voice_clone_batch_file(
                ref_audio_path=resolve_existing_file(args.ref_audio, label="ref audio"),
                text_file=resolve_optional_file(args.text_file),
                texts=args.text,
                ref_text=args.ref_text,
                ref_text_file=resolve_optional_file(args.ref_text_file),
                language=args.language,
                output_prefix=args.output_prefix,
                x_vector_only_mode=args.x_vector_only_mode,
                **extract_qwen_gen_kwargs(args),
            )
            downloaded = None
            if args.download_dir:
                downloaded = download_qwen_audio_files(
                    client=client,
                    stored_files=expect_stored_file_list(payload, "audio_paths"),
                    destination_dir=Path(args.download_dir).expanduser().resolve(),
                )
            emit_json(with_downloaded_field(payload, downloaded))
            return 0

        if args.qwen_command == "narration":
            validate_text_inputs(args.text, args.text_file, field_name="text")
            payload = client.narration(
                text=args.text,
                text_file=resolve_optional_file(args.text_file),
                language=args.language,
                speaker=args.speaker,
                instruct=args.instruct,
                output_name=args.output_name,
                **extract_qwen_gen_kwargs(args),
            )
            downloaded = None
            if args.download_to:
                downloaded = download_qwen_audio_file(
                    client=client,
                    stored=expect_stored_file(payload, "audio"),
                    destination=Path(args.download_to).expanduser().resolve(),
                )
            emit_json(with_downloaded_field(payload, downloaded))
            return 0

        if args.qwen_command == "narration-batch-file":
            payload = client.narration_batch_file(
                text_file=resolve_existing_file(args.text_file, label="text file"),
                language=args.language,
                speaker=args.speaker,
                instruct=args.instruct,
                output_prefix=args.output_prefix,
                **extract_qwen_gen_kwargs(args),
            )
            downloaded = None
            if args.download_dir:
                downloaded = download_qwen_audio_files(
                    client=client,
                    stored_files=expect_stored_file_list(payload, "audio_paths"),
                    destination_dir=Path(args.download_dir).expanduser().resolve(),
                )
            emit_json(with_downloaded_field(payload, downloaded))
            return 0
    finally:
        client.close()

    raise CliError(f"unsupported qwen command: {args.qwen_command}")


def emit_json(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def with_downloaded_field(payload: dict[str, Any], downloaded: str | list[str] | None) -> dict[str, Any]:
    if downloaded is None:
        return payload
    enriched = dict(payload)
    if isinstance(downloaded, list):
        enriched["downloaded_files"] = downloaded
    else:
        enriched["downloaded_to"] = downloaded
    return enriched


def expect_stored_file(payload: dict[str, Any], key: str) -> dict[str, Any]:
    stored = payload.get(key)
    if not isinstance(stored, dict):
        raise CliError(f"response did not include expected object: {key}")
    return stored


def expect_stored_file_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    stored = payload.get(key)
    if not isinstance(stored, list) or not all(isinstance(item, dict) for item in stored):
        raise CliError(f"response did not include expected list: {key}")
    return stored


def validate_text_inputs(text: str | None, text_file: str | None, *, field_name: str) -> None:
    if bool(text) == bool(text_file):
        raise CliError(f"provide exactly one of --{field_name} or --{field_name}-file")


def validate_reference_text_inputs(
    *,
    ref_text: str | None,
    ref_text_file: str | None,
    x_vector_only_mode: bool,
) -> None:
    if ref_text and ref_text_file:
        raise CliError("provide only one of --ref-text or --ref-text-file")
    if not x_vector_only_mode and not (ref_text or ref_text_file):
        raise CliError("qwen voice cloning requires --ref-text/--ref-text-file unless --x-vector-only-mode is set")


def resolve_existing_file(path_value: str, *, label: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.exists():
        raise CliError(f"{label} not found: {path}")
    return path


def resolve_optional_file(path_value: str | None) -> Path | None:
    if not path_value:
        return None
    return resolve_existing_file(path_value, label="file")


def extract_qwen_gen_kwargs(args: argparse.Namespace, prefix: str = "") -> dict[str, Any]:
    key_prefix = prefix_to_dest(prefix)
    do_sample = getattr(args, f"{key_prefix}do_sample", None)
    sampling_keys = (
        "top_k",
        "top_p",
        "temperature",
        "subtalker_top_k",
        "subtalker_top_p",
        "subtalker_temperature",
    )
    sampling_enabled = any(getattr(args, f"{key_prefix}{key}", None) is not None for key in sampling_keys)
    if do_sample is None and sampling_enabled:
        do_sample = True

    kwargs = {
        "max_new_tokens": getattr(args, f"{key_prefix}max_new_tokens", None),
        "do_sample": do_sample,
        "top_k": getattr(args, f"{key_prefix}top_k", None),
        "top_p": getattr(args, f"{key_prefix}top_p", None),
        "temperature": getattr(args, f"{key_prefix}temperature", None),
        "repetition_penalty": getattr(args, f"{key_prefix}repetition_penalty", None),
        "subtalker_top_k": getattr(args, f"{key_prefix}subtalker_top_k", None),
        "subtalker_top_p": getattr(args, f"{key_prefix}subtalker_top_p", None),
        "subtalker_temperature": getattr(args, f"{key_prefix}subtalker_temperature", None),
    }
    if do_sample is False:
        for key in sampling_keys:
            kwargs[key] = None
    return kwargs


def download_qwen_audio_file(*, client: Qwen3TTSClient, stored: dict[str, Any], destination: Path) -> str:
    local_path = try_copy_stored_file(stored=stored, destination=destination)
    if local_path is not None:
        return str(local_path)

    url = stored.get("url")
    if not isinstance(url, str) or not url:
        raise CliError("stored file response did not include a usable url")
    client.download(url, destination)
    return str(destination)


def download_qwen_audio_files(
    *,
    client: Qwen3TTSClient,
    stored_files: list[dict[str, Any]],
    destination_dir: Path,
) -> list[str]:
    destination_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []
    for stored in stored_files:
        filename = str(stored.get("filename") or "audio.wav")
        destination = destination_dir / filename
        downloaded.append(download_qwen_audio_file(client=client, stored=stored, destination=destination))
    return downloaded


def try_copy_stored_file(*, stored: dict[str, Any], destination: Path) -> Path | None:
    path_value = stored.get("path")
    if not isinstance(path_value, str) or not path_value:
        return None
    source = Path(path_value).expanduser().resolve()
    if not source.exists():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def cmd_list_projects(args: argparse.Namespace) -> int:
    content_root = Path(args.content_root).expanduser().resolve()
    if not content_root.exists():
        raise CliError(f"content root not found: {content_root}")

    found = False
    for candidate in sorted(content_root.iterdir()):
        if candidate.is_dir() and (candidate / "descriptor.json").exists():
            print(candidate.name)
            found = True
    if not found:
        raise CliError(f"no projects found under {content_root}")
    return 0


def cmd_clone_project(args: argparse.Namespace) -> int:
    ensure_tool_available("ffmpeg")
    ensure_tool_available("ffprobe")

    project_paths = resolve_project_paths(
        content_root=Path(args.content_root).expanduser().resolve(),
        slug=args.project,
        reference_dir_name=args.reference_dir_name,
    )
    descriptor = read_json(project_paths.descriptor_path)
    items = descriptor.get("content")
    if not isinstance(items, list) or not items:
        raise CliError(f"descriptor content is empty: {project_paths.descriptor_path}")

    original_texts = {item_uid(item): str(item.get(args.text_key, "")).strip() for item in items}
    target_texts = load_text_mapping(items=items, script_file=args.script_file, default_key=args.text_key)
    reference_texts = load_text_mapping(
        items=items,
        script_file=args.reference_script_file,
        default_key=args.text_key,
    )
    if not args.reference_script_file:
        reference_texts = original_texts

    if args.engine == "qwen3tts" and not args.qwen_x_vector_only_mode:
        missing = [uid for uid, text in reference_texts.items() if not text.strip()]
        if missing:
            raise CliError(
                "qwen3tts requires reference text for every item unless --qwen-x-vector-only-mode is set. "
                f"Missing uid(s): {', '.join(missing)}"
            )

    if not args.shared_reference_audio and not args.no_backup_reference_audio:
        backup_project_audio(project_paths, items)

    total = len(items)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] project={project_paths.slug} engine={args.engine} items={total}")

    if args.engine == "indextts":
        client = IndexTTSClient(base_url=args.indextts_url, timeout=args.timeout)
    else:
        client = Qwen3TTSClient(base_url=args.qwen3tts_url, timeout=args.timeout)

    try:
        with tempfile.TemporaryDirectory(prefix="my-tts-") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            if should_use_qwen_batch_mode(args=args, items=items, reference_texts=reference_texts):
                process_qwen_project_batch(
                    client=client,
                    args=args,
                    temp_dir=temp_dir,
                    items=items,
                    project_paths=project_paths,
                    target_texts=target_texts,
                    reference_texts=reference_texts,
                    original_texts=original_texts,
                    timestamp=timestamp,
                )
            else:
                process_project_item_by_item(
                    client=client,
                    args=args,
                    temp_dir=temp_dir,
                    items=items,
                    project_paths=project_paths,
                    target_texts=target_texts,
                    reference_texts=reference_texts,
                    original_texts=original_texts,
                    timestamp=timestamp,
                )
    finally:
        client.close()

    timeline = build_timeline(descriptor)
    write_json(project_paths.descriptor_path, descriptor)
    write_json(project_paths.timeline_path, timeline)
    print(f"updated descriptor: {project_paths.descriptor_path}")
    print(f"updated timeline:   {project_paths.timeline_path}")
    return 0


def should_use_qwen_batch_mode(
    *,
    args: argparse.Namespace,
    items: list[dict[str, Any]],
    reference_texts: dict[str, str],
) -> bool:
    if args.engine != "qwen3tts":
        return False
    if not args.shared_reference_audio:
        return False
    if len(items) < 1:
        return False
    if args.qwen_x_vector_only_mode:
        return True

    unique_ref_texts = {reference_texts[item_uid(item)].strip() for item in items if reference_texts[item_uid(item)].strip()}
    return len(unique_ref_texts) == 1


def process_project_item_by_item(
    *,
    client: IndexTTSClient | Qwen3TTSClient,
    args: argparse.Namespace,
    temp_dir: Path,
    items: list[dict[str, Any]],
    project_paths: ProjectPaths,
    target_texts: dict[str, str],
    reference_texts: dict[str, str],
    original_texts: dict[str, str],
    timestamp: str,
) -> None:
    total = len(items)
    for index, item in enumerate(items, start=1):
        uid = item_uid(item)
        target_text = target_texts[uid].strip()
        reference_text = reference_texts[uid].strip()
        if not target_text:
            raise CliError(f"target text is empty for uid={uid}")

        reference_audio = resolve_reference_audio(
            project_paths=project_paths,
            uid=uid,
            shared_reference_audio=Path(args.shared_reference_audio).expanduser().resolve()
            if args.shared_reference_audio
            else None,
        )
        print(f"[{index}/{total}] uid={uid} ref={reference_audio.name}")

        normalized_reference = temp_dir / f"{uid}_reference.wav"
        transcode_to_wav(reference_audio, normalized_reference)

        result_audio = synthesize_item(
            client=client,
            engine=args.engine,
            uid=uid,
            target_text=target_text,
            reference_text=reference_text,
            reference_audio_wav=normalized_reference,
            temp_dir=temp_dir,
            args=args,
        )
        store_project_result_audio(
            item=item,
            result_audio=result_audio,
            destination_mp3=project_paths.audio_dir / f"{uid}.mp3",
            target_text=target_text,
            original_text=original_texts[uid],
            engine=args.engine,
            reference_audio=reference_audio,
            reference_text=reference_text,
            x_vector_only_mode=args.qwen_x_vector_only_mode,
            timestamp=timestamp,
        )


def process_qwen_project_batch(
    *,
    client: IndexTTSClient | Qwen3TTSClient,
    args: argparse.Namespace,
    temp_dir: Path,
    items: list[dict[str, Any]],
    project_paths: ProjectPaths,
    target_texts: dict[str, str],
    reference_texts: dict[str, str],
    original_texts: dict[str, str],
    timestamp: str,
) -> None:
    assert isinstance(client, Qwen3TTSClient)
    shared_reference_audio = Path(args.shared_reference_audio).expanduser().resolve()
    reference_audio = resolve_reference_audio(
        project_paths=project_paths,
        uid=item_uid(items[0]),
        shared_reference_audio=shared_reference_audio,
    )
    print(f"[batch] using qwen3tts batch endpoint with shared ref={reference_audio.name}")

    normalized_reference = temp_dir / "shared_reference.wav"
    transcode_to_wav(reference_audio, normalized_reference)
    text_file = temp_dir / "batch_lines.txt"
    text_file.write_text(
        "\n".join(target_texts[item_uid(item)].strip() for item in items) + os.linesep,
        encoding="utf-8",
    )
    first_uid = item_uid(items[0])
    common_reference_text = None if args.qwen_x_vector_only_mode else reference_texts[first_uid].strip()
    payload = client.voice_clone_batch_file(
        ref_audio_path=normalized_reference,
        text_file=text_file,
        ref_text=common_reference_text,
        language=args.qwen_language,
        output_prefix=project_paths.slug,
        x_vector_only_mode=args.qwen_x_vector_only_mode,
        **extract_qwen_gen_kwargs(args, prefix="qwen-"),
    )
    stored_files = expect_stored_file_list(payload, "audio_paths")
    if len(stored_files) != len(items):
        raise CliError(f"qwen batch returned {len(stored_files)} files, expected {len(items)}")

    for index, (item, stored) in enumerate(zip(items, stored_files, strict=True), start=1):
        uid = item_uid(item)
        print(f"[{index}/{len(items)}] uid={uid} batch-output={stored.get('filename', 'audio.wav')}")
        fetched_audio = materialize_qwen_stored_audio(
            client=client,
            stored=stored,
            destination=temp_dir / f"{uid}_qwen_batch.wav",
        )
        store_project_result_audio(
            item=item,
            result_audio=fetched_audio,
            destination_mp3=project_paths.audio_dir / f"{uid}.mp3",
            target_text=target_texts[uid].strip(),
            original_text=original_texts[uid],
            engine=args.engine,
            reference_audio=reference_audio,
            reference_text=reference_texts[uid].strip(),
            x_vector_only_mode=args.qwen_x_vector_only_mode,
            timestamp=timestamp,
        )


def store_project_result_audio(
    *,
    item: dict[str, Any],
    result_audio: Path,
    destination_mp3: Path,
    target_text: str,
    original_text: str,
    engine: str,
    reference_audio: Path,
    reference_text: str,
    x_vector_only_mode: bool,
    timestamp: str,
) -> None:
    transcode_to_mp3(result_audio, destination_mp3)
    duration_seconds = probe_duration_seconds(destination_mp3)

    if target_text != original_text and item.get("sourceText") in {None, ""}:
        item["sourceText"] = original_text
    item["text"] = target_text
    item["audioTimestamps"] = build_alignment(target_text, duration_seconds)
    item["ttsEngine"] = engine
    item["ttsMeta"] = {
        "updatedAt": timestamp,
        "referenceAudio": str(reference_audio),
        "referenceText": reference_text if engine == "qwen3tts" and not x_vector_only_mode else None,
        "durationSeconds": round(duration_seconds, 3),
    }


def resolve_project_paths(content_root: Path, slug: str, reference_dir_name: str) -> ProjectPaths:
    root = content_root / slug
    descriptor_path = root / "descriptor.json"
    timeline_path = root / "timeline.json"
    audio_dir = root / "audio"
    image_dir = root / "images"
    reference_audio_dir = root / reference_dir_name

    if not descriptor_path.exists():
        raise CliError(f"descriptor not found: {descriptor_path}")
    if not audio_dir.exists():
        raise CliError(f"audio directory not found: {audio_dir}")

    reference_audio_dir.mkdir(parents=True, exist_ok=True)
    return ProjectPaths(
        slug=slug,
        root=root,
        descriptor_path=descriptor_path,
        timeline_path=timeline_path,
        audio_dir=audio_dir,
        image_dir=image_dir,
        reference_audio_dir=reference_audio_dir,
    )


def backup_project_audio(project_paths: ProjectPaths, items: list[dict[str, Any]]) -> None:
    for item in items:
        uid = item_uid(item)
        current_audio = project_paths.audio_dir / f"{uid}.mp3"
        if not current_audio.exists():
            continue
        backup_audio = project_paths.reference_audio_dir / current_audio.name
        if not backup_audio.exists():
            shutil.copy2(current_audio, backup_audio)


def resolve_reference_audio(project_paths: ProjectPaths, uid: str, shared_reference_audio: Path | None) -> Path:
    if shared_reference_audio is not None:
        if not shared_reference_audio.exists():
            raise CliError(f"shared reference audio not found: {shared_reference_audio}")
        return shared_reference_audio

    backup_audio = project_paths.reference_audio_dir / f"{uid}.mp3"
    if backup_audio.exists():
        return backup_audio

    current_audio = project_paths.audio_dir / f"{uid}.mp3"
    if current_audio.exists():
        return current_audio

    raise CliError(f"reference audio not found for uid={uid}")


def synthesize_item(
    *,
    client: IndexTTSClient | Qwen3TTSClient,
    engine: str,
    uid: str,
    target_text: str,
    reference_text: str,
    reference_audio_wav: Path,
    temp_dir: Path,
    args: argparse.Namespace,
) -> Path:
    if engine == "indextts":
        assert isinstance(client, IndexTTSClient)
        payload = client.synthesize(
            text=target_text,
            prompt_wav_path=str(reference_audio_wav),
            output_name=f"{uid}.wav",
            emo_audio_prompt=args.index_emo_audio_prompt,
            emo_alpha=args.index_emo_alpha,
            emo_text=args.index_emo_text,
            use_random=args.index_use_random,
            verbose=args.index_verbose,
            interval_silence=args.index_interval_silence,
            max_text_tokens_per_segment=args.index_max_text_tokens_per_segment,
        )
        audio_path = Path(str(payload.get("audio_path", ""))).expanduser().resolve()
        if not audio_path.exists():
            raise CliError(
                "indextts returned an audio_path that is not readable from this machine: "
                f"{audio_path}"
            )
        return audio_path

    assert isinstance(client, Qwen3TTSClient)
    payload = client.voice_clone(
        ref_audio_path=reference_audio_wav,
        text=target_text,
        output_name=f"{uid}.wav",
        language=args.qwen_language,
        ref_text=reference_text or None,
        x_vector_only_mode=args.qwen_x_vector_only_mode,
        **extract_qwen_gen_kwargs(args, prefix="qwen-"),
    )
    return materialize_qwen_stored_audio(
        client=client,
        stored=expect_stored_file(payload, "audio"),
        destination=temp_dir / f"{uid}_qwen.wav",
    )


def materialize_qwen_stored_audio(*, client: Qwen3TTSClient, stored: dict[str, Any], destination: Path) -> Path:
    copied = try_copy_stored_file(stored=stored, destination=destination)
    if copied is not None:
        return copied
    url = stored.get("url")
    if not isinstance(url, str) or not url:
        raise CliError("qwen3tts response did not include a usable audio url")
    client.download(url, destination)
    return destination


def load_text_mapping(
    *,
    items: list[dict[str, Any]],
    script_file: str | None,
    default_key: str,
) -> dict[str, str]:
    if not script_file:
        return {item_uid(item): str(item.get(default_key, "")).strip() for item in items}

    path = Path(script_file).expanduser().resolve()
    if not path.exists():
        raise CliError(f"script file not found: {path}")

    raw = read_text_source(path)
    if path.suffix.lower() == ".json":
        return parse_json_text_mapping(raw=raw, items=items, path=path)

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != len(items):
        raise CliError(f"{path} has {len(lines)} non-empty lines, expected {len(items)}")
    return {item_uid(item): text for item, text in zip(items, lines, strict=True)}


def parse_json_text_mapping(*, raw: str, items: list[dict[str, Any]], path: Path) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"invalid JSON in {path}: {exc}") from exc

    item_uids = [item_uid(item) for item in items]
    if isinstance(parsed, dict):
        if isinstance(parsed.get("texts"), list):
            parsed = parsed["texts"]
        else:
            missing = [uid for uid in item_uids if uid not in parsed]
            if missing:
                raise CliError(f"{path} is missing uid(s): {', '.join(missing)}")
            return {uid: str(parsed[uid]).strip() for uid in item_uids}

    if isinstance(parsed, list):
        if len(parsed) != len(items):
            raise CliError(f"{path} has {len(parsed)} entries, expected {len(items)}")
        if all(isinstance(entry, str) for entry in parsed):
            return {item_uid(item): str(text).strip() for item, text in zip(items, parsed, strict=True)}
        if all(isinstance(entry, dict) and "uid" in entry and "text" in entry for entry in parsed):
            normalized = {str(entry["uid"]): str(entry["text"]).strip() for entry in parsed}
            missing = [uid for uid in item_uids if uid not in normalized]
            if missing:
                raise CliError(f"{path} is missing uid(s): {', '.join(missing)}")
            return {uid: normalized[uid] for uid in item_uids}

    raise CliError(
        f"unsupported text file format for {path}. "
        "Use a txt line-per-item file, a JSON array of strings, or a JSON uid->text mapping."
    )


def read_text_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CliError(f"failed to read text as UTF-8: {path}") from exc


def build_alignment(text: str, duration_seconds: float) -> dict[str, list[float] | list[str]]:
    characters = list(text)
    if not characters:
        return {
            "characters": [],
            "characterStartTimesSeconds": [],
            "characterEndTimesSeconds": [],
        }

    unit = duration_seconds / len(characters)
    start_times: list[float] = []
    end_times: list[float] = []
    cursor = 0.0
    for index in range(len(characters)):
        start_times.append(round(cursor, 3))
        cursor = duration_seconds if index == len(characters) - 1 else (index + 1) * unit
        end_times.append(round(cursor, 3))

    return {
        "characters": characters,
        "characterStartTimesSeconds": start_times,
        "characterEndTimesSeconds": end_times,
    }


def build_timeline(descriptor: dict[str, Any]) -> dict[str, Any]:
    timeline: dict[str, Any] = {
        "shortTitle": descriptor.get("shortTitle", ""),
        "elements": [],
        "text": [],
        "audio": [],
    }
    current_ms = 0
    zoom_in = True
    text_animation_seed = 0

    for item in descriptor.get("content", []):
        uid = item_uid(item)
        text = str(item.get("text", "")).strip()
        timestamps = item.get("audioTimestamps") or {}
        end_times = timestamps.get("characterEndTimesSeconds") or []
        if end_times:
            duration_ms = max(1, math.ceil(float(end_times[-1]) * 1000))
        else:
            duration_ms = max(1500, len(text) * 160)

        timeline["elements"].append(
            {
                "startMs": current_ms,
                "endMs": current_ms + duration_ms,
                "imageUrl": uid,
                "enterTransition": "blur",
                "exitTransition": "blur",
                "animations": [
                    {
                        "type": "scale",
                        "from": 1.5 if zoom_in else 1,
                        "to": 1 if zoom_in else 1.5,
                        "startMs": 0,
                        "endMs": duration_ms,
                    }
                ],
            }
        )
        timeline["audio"].append(
            {
                "startMs": current_ms,
                "endMs": current_ms + duration_ms,
                "audioUrl": uid,
            }
        )
        for chunk in build_text_segments(text=text, item_start_ms=current_ms, item_duration_ms=duration_ms):
            chunk["animations"] = build_text_animations(text_animation_seed)
            timeline["text"].append(chunk)
            text_animation_seed += 1

        current_ms += duration_ms
        zoom_in = not zoom_in

    return timeline


def build_text_segments(*, text: str, item_start_ms: int, item_duration_ms: int) -> list[dict[str, Any]]:
    chunks = split_subtitle_chunks(text)
    if not chunks:
        return []

    weights = [max(1, visible_char_count(chunk)) for chunk in chunks]
    total_weight = sum(weights)
    segments: list[dict[str, Any]] = []
    cursor = item_start_ms

    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            next_cursor = item_start_ms + item_duration_ms
        else:
            share = item_duration_ms * (weights[index] / total_weight)
            next_cursor = max(cursor + 1, int(round(cursor + share)))
        segments.append(
            {
                "startMs": cursor,
                "endMs": next_cursor,
                "text": chunk,
                "position": "center",
            }
        )
        cursor = next_cursor

    segments[-1]["endMs"] = item_start_ms + item_duration_ms
    return segments


def split_subtitle_chunks(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text.strip())
    if not normalized:
        return []

    if " " in normalized:
        return chunk_words(normalized, max_chars=16)
    if looks_cjk(normalized):
        return chunk_cjk(normalized, max_chars=12)
    return chunk_cjk(normalized, max_chars=16)


def chunk_words(text: str, *, max_chars: int) -> list[str]:
    words = text.split(" ")
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def chunk_cjk(text: str, *, max_chars: int) -> list[str]:
    sentence_like = re.findall(r"[^，。！？；、,.!?;]+[，。！？；、,.!?;]?", text)
    if not sentence_like:
        sentence_like = [text]

    chunks: list[str] = []
    for piece in sentence_like:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) <= max_chars:
            if chunks and len(chunks[-1]) + len(piece) <= max_chars:
                chunks[-1] += piece
            else:
                chunks.append(piece)
            continue
        for start in range(0, len(piece), max_chars):
            chunks.append(piece[start : start + max_chars])
    return chunks


def build_text_animations(seed: int) -> list[dict[str, int | float | str]]:
    if seed % 2 == 0:
        return [
            {"type": "scale", "from": 0.72, "to": 1.18, "startMs": 0, "endMs": 280},
            {"type": "scale", "from": 1.18, "to": 1.0, "startMs": 280, "endMs": 460},
        ]
    return [{"type": "scale", "from": 0.86, "to": 1.0, "startMs": 0, "endMs": 260}]


def visible_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def looks_cjk(text: str) -> bool:
    return any(
        "\u3040" <= ch <= "\u30ff" or "\u3400" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af"
        for ch in text
    )


def item_uid(item: dict[str, Any]) -> str:
    uid = str(item.get("uid", "")).strip()
    if not uid:
        raise CliError("descriptor item is missing uid")
    return uid


def ensure_tool_available(name: str) -> None:
    if shutil.which(name) is None:
        raise CliError(f"required executable not found in PATH: {name}")


def transcode_to_wav(source: Path, destination: Path) -> None:
    run_subprocess(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def transcode_to_mp3(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_subprocess(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(destination),
        ]
    )


def probe_duration_seconds(path: Path) -> float:
    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(output.stdout.strip())


def run_subprocess(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or exc.stdout.strip()
        raise CliError(f"command failed: {' '.join(command)}\n{stderr}") from exc


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CliError(f"invalid JSON: {path}") from exc


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + os.linesep, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
