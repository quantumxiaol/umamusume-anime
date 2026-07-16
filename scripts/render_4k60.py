#!/usr/bin/env python3
"""Safe, resumable 4K60 Remotion renderer for Uma Musume projects.

The renderer deliberately keeps orchestration outside Remotion:

* one repository-wide render lock;
* the selected local TTS backend is health-checked and gracefully stopped;
* macOS memory pressure and scratch space are checked before rendering;
* a minimal public directory is bundled exactly once per invocation;
* bounded chunks may run in a controlled outer worker pool;
* chunks are resumed only after media and render-signature validation;
* final PCM is rebuilt with an exact sample count, while the muxed AAC track is
  validated to within one millisecond and fully decoded.

This module uses only the Python standard library for rendering orchestration.
The TTS gate imports the repository-local ``my_tts`` client so the shutdown
protocol stays in one place.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import errno
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import urlsplit


RENDERER_VERSION = 1
DEFAULT_LOCK_DIR = Path("/private/tmp/umamusume-anime-4k-render.lock")
DEFAULT_TEMP_ROOT = Path("/private/tmp")
MAX_OUTER_JOBS = 4
MAX_TOTAL_RENDER_WORKERS = 4
AAC_DURATION_TOLERANCE_SAMPLES = 4096
PROCESS_TERMINATION_GRACE_SECONDS = 5.0

_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_ACTIVE_PROCESSES_LOCK = threading.Lock()


class RenderError(RuntimeError):
    """Expected render failure with a concise user-facing message."""


@dataclass(frozen=True, slots=True)
class RenderConfig:
    repo_root: Path
    project: str
    composition: str
    output_slug: str
    output_path: Path
    tts_engine: str
    tts_url: str
    jobs: int = 1
    render_concurrency: int = 1
    fps: int = 60
    intro_frames: int = 60
    chunk_frames: int = 3000
    width: int = 3840
    height: int = 2160
    crf: int = 18
    x264_preset: str = "medium"
    pixel_format: str = "yuv420p"
    expected_pixel_format: str = "yuvj420p"
    hardware_acceleration: str = "disable"
    chunk_audio_codec: str = "aac"
    final_audio_codec: str = "aac"
    chunk_audio_bitrate: str = "320k"
    final_audio_bitrate: str = "192k"
    sample_rate: int = 48000
    min_scratch_gib: int = 20
    min_memory_free_percent: int = 40
    tts_timeout: float = 5.0
    tts_wait_timeout: float = 60.0
    lock_dir: Path = DEFAULT_LOCK_DIR
    temp_root: Path = DEFAULT_TEMP_ROOT
    keep_temp: bool = False
    keep_assembly: bool = False

    @property
    def video_root(self) -> Path:
        return self.repo_root / "my-video"

    @property
    def project_root(self) -> Path:
        return self.video_root / "public" / "content" / self.project

    @property
    def timeline_path(self) -> Path:
        return self.project_root / "timeline.json"

    @property
    def chunk_dir(self) -> Path:
        return self.video_root / "out" / f"{self.output_slug}-chunks"

    @property
    def log_dir(self) -> Path:
        return self.video_root / "out" / f"{self.output_slug}-render-logs"

    @property
    def assembly_dir(self) -> Path:
        return self.video_root / "out" / f"{self.output_slug}-assembly"

    @property
    def total_render_workers(self) -> int:
        return self.jobs * self.render_concurrency

    @property
    def required_memory_free_percent(self) -> int:
        # The documented 40-49% range is serial-only. Each additional active
        # Chromium renderer requires another ten percentage points of headroom.
        return max(
            self.min_memory_free_percent,
            40 + 10 * (self.total_render_workers - 1),
        )

    @property
    def required_scratch_gib(self) -> int:
        # --disallow-parallel-encoding stores frames for every active chunk.
        return self.min_scratch_gib * self.jobs


@dataclass(frozen=True, slots=True)
class TimelineInfo:
    item_count: int
    last_end_ms: int
    image_names: tuple[str, ...]
    audio_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    start: int
    end: int

    @property
    def frame_count(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class ChunkPaths:
    media: Path
    partial_media: Path
    signature: Path
    partial_signature: Path
    log: Path


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def percentage(value: str) -> int:
    parsed = int(value)
    if parsed < 0 or parsed > 100:
        raise argparse.ArgumentTypeError("value must be from 0 to 100")
    return parsed


def derive_composition(project: str) -> str:
    """Mirror Root.tsx's composition-id normalization."""

    return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff-]", "-", project)


def derive_output_slug(project: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", project).strip("-").lower()
    if not slug:
        raise RenderError(f"cannot derive an output slug from project name: {project!r}")
    return slug


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render one Director project as a validated, resumable 4K60 MP4.",
    )
    parser.add_argument("--project", required=True, help="Directory name under my-video/public/content.")
    parser.add_argument(
        "--composition",
        help="Remotion composition id. Defaults to the project name with unsupported characters replaced by '-'.",
    )
    parser.add_argument("--output-slug", help="Stem used for chunks, logs, assembly files, and default MP4 name.")
    parser.add_argument("--output", help="Final MP4 path. Defaults to my-video/out/<output-slug>.mp4.")
    parser.add_argument(
        "--tts-engine",
        choices=("fishspeech", "qwen3tts"),
        default=os.environ.get("TTS_ENGINE"),
        help="Backend used for synthesis; may also be provided through TTS_ENGINE.",
    )
    parser.add_argument(
        "--tts-url",
        default=os.environ.get("TTS_URL"),
        help="Loopback URL used for synthesis; may also be provided through TTS_URL.",
    )
    parser.add_argument(
        "--jobs",
        type=positive_int,
        default=1,
        help=f"Concurrent chunk processes after TTS shutdown (default 1, maximum {MAX_OUTER_JOBS}).",
    )
    parser.add_argument(
        "--render-concurrency",
        type=positive_int,
        default=1,
        help="Remotion concurrency inside each chunk process (default 1).",
    )
    parser.add_argument("--chunk-frames", type=positive_int, default=3000)
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--x264-preset", default="medium")
    parser.add_argument("--min-scratch-gib", type=positive_int, default=20)
    parser.add_argument("--min-memory-free-percent", type=percentage, default=40)
    parser.add_argument("--tts-timeout", type=float, default=5.0)
    parser.add_argument("--tts-wait-timeout", type=float, default=60.0)
    parser.add_argument("--lock-dir", type=Path, default=DEFAULT_LOCK_DIR)
    parser.add_argument("--temp-root", type=Path, default=DEFAULT_TEMP_ROOT)
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the minimal public directory and bundle for debugging.",
    )
    parser.add_argument(
        "--keep-assembly",
        action="store_true",
        help="Keep intermediate PCM, concat lists, and mux files after a successful publication.",
    )
    return parser


def config_from_args(args: argparse.Namespace, *, repo_root: Path | None = None) -> RenderConfig:
    if not args.tts_engine:
        raise RenderError("--tts-engine is required (or set TTS_ENGINE)")
    if not args.tts_url:
        raise RenderError("--tts-url is required (or set TTS_URL)")

    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    output_slug = args.output_slug or derive_output_slug(args.project)
    output_path = (
        Path(args.output).expanduser()
        if args.output
        else root / "my-video" / "out" / f"{output_slug}.mp4"
    )
    if not output_path.is_absolute():
        output_path = root / output_path

    config = RenderConfig(
        repo_root=root,
        project=args.project,
        composition=args.composition or derive_composition(args.project),
        output_slug=output_slug,
        output_path=output_path.resolve(),
        tts_engine=args.tts_engine,
        tts_url=args.tts_url,
        jobs=args.jobs,
        render_concurrency=args.render_concurrency,
        chunk_frames=args.chunk_frames,
        crf=args.crf,
        x264_preset=args.x264_preset,
        min_scratch_gib=args.min_scratch_gib,
        min_memory_free_percent=args.min_memory_free_percent,
        tts_timeout=args.tts_timeout,
        tts_wait_timeout=args.tts_wait_timeout,
        lock_dir=args.lock_dir.expanduser().resolve(),
        temp_root=args.temp_root.expanduser().resolve(),
        keep_temp=args.keep_temp,
        keep_assembly=args.keep_assembly,
    )
    validate_config(config)
    return config


def validate_config(config: RenderConfig) -> None:
    if Path(config.project).name != config.project or config.project in {".", ".."}:
        raise RenderError(f"--project must be a directory basename: {config.project!r}")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", config.output_slug):
        raise RenderError(
            "--output-slug may contain only ASCII letters, digits, dot, underscore, and hyphen"
        )
    if config.jobs > MAX_OUTER_JOBS:
        raise RenderError(f"--jobs must be at most {MAX_OUTER_JOBS}, got {config.jobs}")
    if config.total_render_workers > MAX_TOTAL_RENDER_WORKERS:
        raise RenderError(
            "jobs × render-concurrency must be at most "
            f"{MAX_TOTAL_RENDER_WORKERS}, got {config.jobs} × {config.render_concurrency}"
        )
    if config.min_memory_free_percent < 40:
        raise RenderError("--min-memory-free-percent must be at least 40 for 4K60")
    if config.min_scratch_gib < 20:
        raise RenderError("--min-scratch-gib must be at least 20 for bounded 4K60 chunks")
    if config.crf < 0 or config.crf > 51:
        raise RenderError(f"--crf must be from 0 to 51, got {config.crf}")
    if config.tts_timeout <= 0 or not math.isfinite(config.tts_timeout):
        raise RenderError("--tts-timeout must be finite and greater than zero")
    if config.tts_wait_timeout <= 0 or not math.isfinite(config.tts_wait_timeout):
        raise RenderError("--tts-wait-timeout must be finite and greater than zero")
    if not config.composition:
        raise RenderError("composition id cannot be empty")
    if "_" in config.composition:
        raise RenderError(f"Remotion composition ids cannot contain underscores: {config.composition}")
    if not re.fullmatch(r"[a-zA-Z0-9\u4e00-\u9fff-]+", config.composition):
        raise RenderError(f"unsupported character in Remotion composition id: {config.composition!r}")
    parse_loopback_endpoint(config.tts_url)


def _register_process(process: subprocess.Popen[str]) -> None:
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.add(process)


def _unregister_process(process: subprocess.Popen[str]) -> None:
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.discard(process)


def _signal_process_group(process: subprocess.Popen[str], signum: signal.Signals) -> None:
    if process.poll() is not None:
        return
    try:
        # Every child is started in a fresh session, so its pid is also the
        # process-group id. This reaches Chromium/ffmpeg descendants as well.
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def terminate_active_processes(*, grace_seconds: float = PROCESS_TERMINATION_GRACE_SECONDS) -> None:
    """Stop every registered command and its descendants, escalating if needed."""

    with _ACTIVE_PROCESSES_LOCK:
        processes = tuple(_ACTIVE_PROCESSES)
    for process in processes:
        _signal_process_group(process, signal.SIGTERM)

    deadline = time.monotonic() + grace_seconds
    while any(process.poll() is None for process in processes) and time.monotonic() < deadline:
        time.sleep(0.05)
    for process in processes:
        _signal_process_group(process, signal.SIGKILL)


def run_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    log_path: Path | None = None,
    check: bool = True,
    cancellation_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command in its own process group and track it for prompt cleanup."""

    args = list(command)
    if cancellation_event is not None and cancellation_event.is_set():
        raise RenderError(f"command cancelled before launch: {' '.join(args)}")

    handle = None
    try:
        if log_path is None:
            stdout_target: Any = subprocess.PIPE
            stderr_target: Any = subprocess.PIPE
        else:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handle = log_path.open("w", encoding="utf-8")
            stdout_target = handle
            stderr_target = subprocess.STDOUT

        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=stdout_target,
            stderr=stderr_target,
            text=True,
            start_new_session=True,
        )
        _register_process(process)
        try:
            if cancellation_event is not None and cancellation_event.is_set():
                _signal_process_group(process, signal.SIGTERM)
            stdout, stderr = process.communicate()
        finally:
            _unregister_process(process)

        completed = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check and process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode,
                args,
                output=stdout,
                stderr=stderr,
            )
        return completed
    finally:
        if handle is not None:
            handle.close()


def command_error(exc: subprocess.CalledProcessError) -> str:
    detail = (exc.stderr or exc.stdout or "").strip()
    rendered = " ".join(str(part) for part in exc.cmd)
    return f"command failed ({exc.returncode}): {rendered}" + (f"\n{detail}" if detail else "")


def validate_required_tools() -> None:
    missing = [
        tool
        for tool in ("ffmpeg", "ffprobe", "lsof", "memory_pressure", "node", "pnpm")
        if shutil.which(tool) is None
    ]
    if missing:
        raise RenderError(f"missing required render tools: {', '.join(missing)}")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _millisecond(value: object, *, label: str) -> int:
    if not _is_number(value) or float(value) != math.floor(float(value)):
        raise RenderError(f"{label} must be an integer millisecond value")
    return int(value)


def _safe_asset_name(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RenderError(f"{label} must be a non-empty string")
    if Path(value).name != value or value in {".", ".."}:
        raise RenderError(f"{label} must be a basename without path traversal: {value!r}")
    return value


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RenderError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RenderError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RenderError(f"expected a JSON object in {path}")
    return payload


def validate_timeline(config: RenderConfig) -> TimelineInfo:
    timeline = read_json_object(config.timeline_path)
    if timeline.get("width") != config.width or timeline.get("height") != config.height:
        raise RenderError(
            f"timeline must be {config.width}x{config.height}, got "
            f"{timeline.get('width')}x{timeline.get('height')}"
        )

    elements = timeline.get("elements")
    text = timeline.get("text")
    audio = timeline.get("audio")
    if not all(isinstance(items, list) for items in (elements, text, audio)):
        raise RenderError("timeline elements, text, and audio must all be arrays")
    assert isinstance(elements, list) and isinstance(text, list) and isinstance(audio, list)
    if not elements:
        raise RenderError("timeline must contain at least one element")

    image_names: list[str] = []
    element_ends: dict[int, int] = {}
    previous_end = 0
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            raise RenderError(f"elements[{index}] must be an object")
        element_start = _millisecond(element.get("startMs"), label=f"elements[{index}].startMs")
        element_end = _millisecond(element.get("endMs"), label=f"elements[{index}].endMs")
        if element_start < 0 or element_end <= element_start:
            raise RenderError(f"invalid element timing at index {index}")
        if element_start < previous_end:
            raise RenderError(f"timeline elements overlap or are out of order at index {index}")
        image_names.append(
            _safe_asset_name(element.get("imageUrl"), label=f"elements[{index}].imageUrl")
        )
        element_ends[element_start] = element_end
        previous_end = element_end

    text_ids: list[str] = []
    text_by_start: dict[int, str | None] = {}
    for index, subtitle in enumerate(text):
        if not isinstance(subtitle, dict):
            raise RenderError(f"text[{index}] must be an object")
        text_start = _millisecond(subtitle.get("startMs"), label=f"text[{index}].startMs")
        text_end = _millisecond(subtitle.get("endMs"), label=f"text[{index}].endMs")
        element_end = element_ends.get(text_start)
        if element_end is None:
            raise RenderError(f"text[{index}] does not start with a timeline element")
        if text_start in text_by_start:
            raise RenderError(f"multiple subtitles start at {text_start}ms")
        if text_end <= text_start or text_end > element_end:
            raise RenderError(f"subtitle timing exceeds element timing at index {index}")
        text_id = (
            _safe_asset_name(subtitle.get("id"), label=f"text[{index}].id")
            if "id" in subtitle
            else None
        )
        if text_id is not None:
            text_ids.append(text_id)
        text_by_start[text_start] = text_id

    audio_names: list[str] = []
    audio_by_start: dict[int, str] = {}
    for index, sound in enumerate(audio):
        if not isinstance(sound, dict):
            raise RenderError(f"audio[{index}] must be an object")
        audio_start = _millisecond(sound.get("startMs"), label=f"audio[{index}].startMs")
        audio_end = _millisecond(sound.get("endMs"), label=f"audio[{index}].endMs")
        element_end = element_ends.get(audio_start)
        if element_end is None:
            raise RenderError(f"audio[{index}] does not start with a timeline element")
        if audio_start in audio_by_start:
            raise RenderError(f"multiple audio items start at {audio_start}ms")
        if audio_end <= audio_start or audio_end > element_end:
            raise RenderError(f"audio timing exceeds element timing at index {index}")
        audio_name = _safe_asset_name(sound.get("audioUrl"), label=f"audio[{index}].audioUrl")
        audio_names.append(audio_name)
        audio_by_start[audio_start] = audio_name

    if len(set(text_ids)) != len(text_ids):
        raise RenderError("timeline subtitle ids must be unique")
    if len(set(audio_names)) != len(audio_names):
        raise RenderError("timeline audio ids must be unique")
    for start in text_by_start.keys() & audio_by_start.keys():
        if text_by_start[start] is not None and text_by_start[start] != audio_by_start[start]:
            raise RenderError(
                f"subtitle id and audio id differ at {start}ms: "
                f"{text_by_start[start]}/{audio_by_start[start]}"
            )
    # image_names intentionally need not be unique: Director may reuse one
    # prepared 4K composition for many consecutive subtitle/audio items.
    for image_name in sorted(set(image_names)):
        image_path = config.project_root / "images" / f"{image_name}.png"
        if not image_path.is_file():
            raise RenderError(f"missing timeline image: {image_path}")
    for audio_name in sorted(set(audio_names)):
        audio_path = config.project_root / "audio" / f"{audio_name}.mp3"
        if not audio_path.is_file():
            raise RenderError(f"missing timeline audio: {audio_path}")

    return TimelineInfo(
        item_count=len(elements),
        last_end_ms=previous_end,
        image_names=tuple(image_names),
        audio_names=tuple(audio_names),
    )


def parse_memory_free_percent(output: str) -> int:
    match = re.search(r"System-wide memory free percentage:\s*([0-9]+)%", output)
    if match is None:
        raise RenderError("memory_pressure output did not contain a free percentage")
    value = int(match.group(1))
    if value < 0 or value > 100:
        raise RenderError(f"memory_pressure free percentage is out of range: {value}")
    return value


def get_memory_free_percent() -> int:
    try:
        result = run_command(["memory_pressure", "-Q"])
    except subprocess.CalledProcessError as exc:
        raise RenderError(command_error(exc)) from exc
    output = (result.stdout or "") + (result.stderr or "")
    value = parse_memory_free_percent(output)
    print(output.strip())
    return value


def parse_loopback_endpoint(url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RenderError(f"invalid TTS URL: {url}") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise RenderError(f"TTS URL must be an HTTP(S) loopback URL: {url}")
    normalized_host = host.rstrip(".").casefold()
    is_loopback = normalized_host == "localhost"
    if not is_loopback:
        try:
            address = ipaddress.ip_address(normalized_host.split("%", 1)[0])
        except ValueError:
            address = None
        if address is not None:
            is_loopback = address.is_loopback
            mapped = getattr(address, "ipv4_mapped", None)
            is_loopback = is_loopback or bool(mapped and mapped.is_loopback)
    if not is_loopback:
        raise RenderError(f"TTS URL must be an explicit loopback URL: {url}")
    return host, port or (443 if parsed.scheme == "https" else 80)


def confirm_listener_stopped(port: int) -> None:
    result = run_command(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        check=False,
    )
    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode == 0:
        raise RenderError(f"TTS listener is still active on port {port}:\n{output or error}")
    if result.returncode != 1 or output or error:
        raise RenderError(
            f"unable to confirm that the TTS listener is gone on port {port}: "
            f"exit={result.returncode} {output or error}"
        )
    print(f"TTS listener confirmation: port={port} state=stopped")


def stop_selected_tts(
    config: RenderConfig,
    *,
    client_factory: Callable[[str, str, float], Any] | None = None,
    listener_checker: Callable[[int], None] = confirm_listener_stopped,
) -> None:
    """Health-check and gracefully stop only the backend selected for this run."""

    try:
        import httpx
        from my_tts.cli import CliError, FishSpeechClient, Qwen3TTSClient
    except ImportError as exc:
        raise RenderError("project TTS client is unavailable; run this script through `uv run`") from exc

    host, port = parse_loopback_endpoint(config.tts_url)
    try:
        if client_factory is None:
            classes = {
                "fishspeech": FishSpeechClient,
                "qwen3tts": Qwen3TTSClient,
            }
            client = classes[config.tts_engine](base_url=config.tts_url, timeout=config.tts_timeout)
        else:
            client = client_factory(config.tts_engine, config.tts_url, config.tts_timeout)
    except Exception as exc:
        raise RenderError(f"unable to initialize {config.tts_engine} shutdown client: {exc}") from exc

    try:
        try:
            health = client.health()
        except httpx.ConnectError:
            print(f"TTS shutdown gate: backend={config.tts_engine} endpoint={host}:{port} state=already-stopped")
            listener_checker(port)
            return
        except httpx.HTTPError as exc:
            raise RenderError(
                f"cannot establish selected TTS state for {config.tts_engine} at {config.tts_url}: {exc}"
            ) from exc
        except CliError as exc:
            raise RenderError(f"{config.tts_engine} health protocol failed: {exc}") from exc
        except Exception as exc:
            raise RenderError(f"unexpected {config.tts_engine} health failure: {exc}") from exc

        if not isinstance(health, dict) or health.get("status") != "ok":
            raise RenderError(f"unexpected {config.tts_engine} health response: {health!r}")
        resident = (
            bool(health.get("loaded"))
            if config.tts_engine == "fishspeech"
            else bool(health.get("loaded_models"))
        )
        print(
            f"TTS health: backend={config.tts_engine} endpoint={host}:{port} "
            f"resident_model={str(resident).lower()}"
        )
        try:
            payload = client.shutdown(wait=True, wait_timeout=config.tts_wait_timeout)
        except httpx.HTTPError as exc:
            raise RenderError(f"graceful {config.tts_engine} shutdown failed: {exc}") from exc
        except CliError as exc:
            raise RenderError(f"graceful {config.tts_engine} shutdown protocol failed: {exc}") from exc
        except Exception as exc:
            raise RenderError(f"unexpected {config.tts_engine} shutdown failure: {exc}") from exc
        if not isinstance(payload, dict):
            raise RenderError(f"unexpected {config.tts_engine} shutdown response: {payload!r}")
        if payload.get("status") not in {"accepted", "already_pending"}:
            raise RenderError(f"{config.tts_engine} did not accept graceful shutdown: {payload!r}")
        if payload.get("server_stopped") is not True:
            raise RenderError(f"{config.tts_engine} shutdown did not confirm server exit: {payload!r}")
        print(f"TTS graceful shutdown: backend={config.tts_engine} status={payload.get('status')}")
        listener_checker(port)
    finally:
        try:
            client.close()
        except Exception as exc:
            # Listener confirmation is the authoritative safety check. A close
            # failure must never escape the renderer's expected error boundary.
            print(f"warning: unable to close {config.tts_engine} client cleanly: {exc}", file=sys.stderr)


def check_scratch_space(config: RenderConfig) -> None:
    required_bytes = config.required_scratch_gib * 1024**3
    paths = tuple(
        dict.fromkeys(
            (
                config.temp_root,
                config.video_root / "out",
                config.output_path.parent,
            )
        )
    )
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(path)
        free_gib = usage.free / 1024**3
        print(f"scratch space: path={path} free={free_gib:.1f}GiB required={config.required_scratch_gib}GiB")
        if usage.free < required_bytes:
            raise RenderError(
                f"insufficient scratch space at {path}: "
                f"free={free_gib:.1f}GiB required={config.required_scratch_gib}GiB"
            )


class AtomicRenderLock:
    def __init__(self, path: Path, *, project: str, composition: str) -> None:
        self.path = path
        self.project = project
        self.composition = composition
        self.held = False

    def __enter__(self) -> AtomicRenderLock:
        try:
            self.path.mkdir()
        except FileExistsError as exc:
            owner = self.path / "owner.json"
            detail = owner.read_text(encoding="utf-8").strip() if owner.is_file() else "owner unknown"
            raise RenderError(
                f"another 4K render appears active; lock exists: {self.path}\n"
                f"{detail}\nDo not remove it until the owner process is confirmed stopped."
            ) from exc
        self.held = True
        try:
            atomic_write_text(
                self.path / "owner.json",
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "project": self.project,
                        "composition": self.composition,
                        "startedUtc": datetime.now(UTC).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + os.linesep,
            )
        except BaseException:
            (self.path / "owner.json").unlink(missing_ok=True)
            self.path.rmdir()
            self.held = False
            raise
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if not self.held:
            return
        owner = self.path / "owner.json"
        try:
            owner.unlink(missing_ok=True)
            self.path.rmdir()
        except OSError as exc:
            print(f"warning: unable to release render lock cleanly: {self.path}: {exc}", file=sys.stderr)
        finally:
            self.held = False


@contextmanager
def signal_cleanup() -> Iterator[None]:
    previous: dict[signal.Signals, Any] = {}

    def terminate(signum: int, _frame: object) -> None:
        terminate_active_processes()
        raise SystemExit(128 + signum)

    for name in ("SIGHUP", "SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, terminate)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}:
                raise
    finally:
        os.close(descriptor)


def publish_file_atomically(source: Path, destination: Path) -> None:
    """Publish a verified file atomically, including across filesystem volumes."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    source_size = source.stat().st_size
    with source.open("rb") as handle:
        os.fsync(handle.fileno())
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    else:
        _fsync_directory(destination.parent)
        return

    staging: Path | None = None
    try:
        with source.open("rb") as input_handle, tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as output_handle:
            staging = Path(output_handle.name)
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        if staging.stat().st_size != source_size:
            raise RenderError(
                f"cross-volume publication copy size mismatch: "
                f"source={source_size} staging={staging.stat().st_size}"
            )
        os.replace(staging, destination)
        staging = None
        if destination.stat().st_size != source_size:
            raise RenderError(f"published file size mismatch: {destination}")
        _fsync_directory(destination.parent)
        source.unlink()
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


@contextmanager
def minimal_bundle_workspace(config: RenderConfig) -> Iterator[tuple[Path, Path]]:
    config.temp_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"{config.output_slug}-", dir=config.temp_root))
    public_root = root / "public"
    content_root = public_root / "content"
    bundle_root = root / "bundle"
    content_root.mkdir(parents=True)
    project_link = content_root / config.project
    project_link.symlink_to(config.project_root, target_is_directory=True)
    print(f"minimal public root: {project_link} -> {config.project_root}")
    try:
        yield public_root, bundle_root
    finally:
        if config.keep_temp:
            print(f"temporary render workspace kept at: {root}")
        else:
            shutil.rmtree(root, ignore_errors=False)


def bundle_command(config: RenderConfig, public_root: Path, bundle_root: Path) -> list[str]:
    return [
        "pnpm",
        "exec",
        "remotion",
        "bundle",
        "src/index.ts",
        f"--public-dir={public_root}",
        f"--out-dir={bundle_root}",
        "--disable-git-source",
    ]


def build_bundle(config: RenderConfig, public_root: Path, bundle_root: Path) -> None:
    log_path = config.log_dir / "bundle.log"
    print(f"bundle Remotion once: output={bundle_root}")
    try:
        run_command(
            bundle_command(config, public_root, bundle_root),
            cwd=config.video_root,
            log_path=log_path,
        )
    except subprocess.CalledProcessError as exc:
        raise RenderError(f"Remotion bundle failed; see {log_path}\n{tail_file(log_path)}") from exc
    if not (bundle_root / "index.html").is_file():
        raise RenderError(f"Remotion bundle did not create index.html: {bundle_root}")


def hash_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def hash_paths(paths: Sequence[Path], *, root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hash_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def source_files(config: RenderConfig) -> list[Path]:
    files = [path for path in (config.video_root / "src").rglob("*") if path.is_file()]
    for name in ("remotion.config.ts", "package.json", "pnpm-lock.yaml", "tsconfig.json"):
        path = config.video_root / name
        if not path.is_file():
            raise RenderError(f"missing Remotion source/config file: {path}")
        files.append(path)
    return files


def build_render_signature(config: RenderConfig, timeline: TimelineInfo) -> str:
    asset_paths = [
        *(config.project_root / "images" / f"{name}.png" for name in set(timeline.image_names)),
        *(config.project_root / "audio" / f"{name}.mp3" for name in set(timeline.audio_names)),
    ]
    payload = {
        "rendererVersion": RENDERER_VERSION,
        "composition": config.composition,
        "items": timeline.item_count,
        "fps": config.fps,
        "introFrames": config.intro_frames,
        "chunkFrames": config.chunk_frames,
        "width": config.width,
        "height": config.height,
        "pixelFormat": config.pixel_format,
        "expectedPixelFormat": config.expected_pixel_format,
        "codec": "h264",
        "crf": config.crf,
        "x264Preset": config.x264_preset,
        "hardwareAcceleration": config.hardware_acceleration,
        "audioCodec": config.chunk_audio_codec,
        "audioBitrate": config.chunk_audio_bitrate,
        "finalAudioCodec": config.final_audio_codec,
        "finalAudioBitrate": config.final_audio_bitrate,
        "sampleRate": config.sample_rate,
        "parallelEncoding": False,
        "timelineSha256": hash_file(config.timeline_path),
        "assetsSha256": hash_paths(asset_paths, root=config.repo_root),
        "remotionSha256": hash_paths(source_files(config), root=config.repo_root),
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_chunks(total_frames: int, chunk_frames: int) -> list[Chunk]:
    if total_frames <= 0:
        raise RenderError(f"total frame count must be positive, got {total_frames}")
    chunks: list[Chunk] = []
    for index, start in enumerate(range(0, total_frames, chunk_frames)):
        chunks.append(Chunk(index=index, start=start, end=min(total_frames - 1, start + chunk_frames - 1)))
    return chunks


def chunk_paths(config: RenderConfig, chunk: Chunk) -> ChunkPaths:
    stem = f"chunk-{chunk.index:03d}-{chunk.start}-{chunk.end}"
    return ChunkPaths(
        media=config.chunk_dir / f"{stem}.mp4",
        partial_media=config.chunk_dir / f"{stem}.partial.mp4",
        signature=config.chunk_dir / f"{stem}.mp4.render-signature",
        partial_signature=config.chunk_dir / f"{stem}.partial.mp4.render-signature",
        log=config.log_dir / f"chunk-{chunk.index:03d}.log",
    )


def signature_matches(path: Path, signature: str) -> bool:
    try:
        return path.read_text(encoding="utf-8").strip() == signature
    except FileNotFoundError:
        return False


def probe_media(path: Path) -> dict[str, Any]:
    try:
        result = run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_entries",
                (
                    "stream=index,codec_type,codec_name,codec_tag_string,profile,width,height,pix_fmt,"
                    "r_frame_rate,avg_frame_rate,nb_frames,nb_read_frames,sample_rate,channels,"
                    "channel_layout,time_base,duration,duration_ts:stream_tags=encoder"
                ),
                "-of",
                "json",
                str(path),
            ]
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RenderError(f"unable to probe media: {path}") from exc
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RenderError(f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(payload, dict):
        raise RenderError(f"ffprobe returned an invalid object for {path}")
    return payload


def _stream(payload: dict[str, Any], kind: str) -> dict[str, Any] | None:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    return next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == kind),
        None,
    )


def _int_value(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def media_is_valid(
    config: RenderConfig,
    path: Path,
    *,
    expected_frames: int,
    expected_audio_codec: str,
    label: str,
) -> bool:
    if not path.is_file():
        return False
    try:
        payload = probe_media(path)
    except RenderError as exc:
        print(f"{label} validation failed: {exc}", file=sys.stderr)
        return False
    video = _stream(payload, "video")
    audio = _stream(payload, "audio")
    if video is None or audio is None:
        return False
    encoder = str((video.get("tags") or {}).get("encoder", "")) if isinstance(video.get("tags"), dict) else ""
    frame_count = _int_value(video.get("nb_read_frames") or video.get("nb_frames"))
    expected_sample_numerator = expected_frames * config.sample_rate
    expected_audio_samples = (
        expected_sample_numerator // config.fps
        if expected_sample_numerator % config.fps == 0
        else None
    )
    actual_audio_samples = _int_value(audio.get("duration_ts"))
    audio_sample_delta = (
        abs(actual_audio_samples - expected_audio_samples)
        if actual_audio_samples is not None and expected_audio_samples is not None
        else None
    )
    valid = all(
        (
            video.get("codec_name") == "h264",
            video.get("codec_tag_string") == "avc1",
            "libx264" in encoder,
            video.get("width") == config.width,
            video.get("height") == config.height,
            video.get("r_frame_rate") == f"{config.fps}/1",
            video.get("avg_frame_rate") == f"{config.fps}/1",
            video.get("pix_fmt") == config.expected_pixel_format,
            frame_count == expected_frames,
            audio.get("codec_name") == expected_audio_codec,
            str(audio.get("sample_rate")) == str(config.sample_rate),
            audio.get("channels") == 2,
            audio.get("time_base") == f"1/{config.sample_rate}",
            audio_sample_delta is not None
            and audio_sample_delta <= AAC_DURATION_TOLERANCE_SAMPLES,
        )
    )
    print(
        f"{label} probe: path={path} codec={video.get('codec_name')} encoder={encoder or 'n/a'} "
        f"size={video.get('width')}x{video.get('height')} fps={video.get('r_frame_rate')} "
        f"pix_fmt={video.get('pix_fmt')} frames={frame_count} "
        f"audio={audio.get('codec_name')}/{audio.get('sample_rate')}Hz/{audio.get('channels')}ch "
        f"audio_samples={actual_audio_samples}/{expected_audio_samples} delta={audio_sample_delta} "
        f"valid={str(valid).lower()}"
    )
    return valid


def prepare_chunk(
    config: RenderConfig,
    chunk: Chunk,
    signature: str,
    *,
    validator: Callable[..., bool] = media_is_valid,
) -> bool:
    """Return True when a chunk needs rendering; recover matching partials."""

    paths = chunk_paths(config, chunk)
    if paths.media.is_file():
        if not signature_matches(paths.signature, signature):
            print(f"rerender chunk {chunk.index}: render signature missing/stale")
        else:
            valid = validator(
                config,
                paths.media,
                expected_frames=chunk.frame_count,
                expected_audio_codec=config.chunk_audio_codec,
                label="chunk",
            )
            if valid:
                print(f"skip chunk {chunk.index}: frames={chunk.start}-{chunk.end} signature=match")
                return False
            print(f"rerender chunk {chunk.index}: media specification mismatch")

    if paths.partial_media.is_file() and signature_matches(paths.partial_signature, signature):
        if validator(
            config,
            paths.partial_media,
            expected_frames=chunk.frame_count,
            expected_audio_codec=config.chunk_audio_codec,
            label="partial chunk",
        ):
            os.replace(paths.partial_media, paths.media)
            atomic_write_text(paths.signature, signature + os.linesep)
            paths.partial_signature.unlink(missing_ok=True)
            print(f"recover chunk {chunk.index}: frames={chunk.frame_count} signature=match")
            return False

    paths.partial_media.unlink(missing_ok=True)
    paths.partial_signature.unlink(missing_ok=True)
    return True


def render_command(config: RenderConfig, bundle_root: Path, chunk: Chunk, output: Path) -> list[str]:
    return [
        "pnpm",
        "exec",
        "remotion",
        "render",
        str(bundle_root),
        config.composition,
        str(output),
        "--codec=h264",
        f"--pixel-format={config.pixel_format}",
        f"--crf={config.crf}",
        f"--x264-preset={config.x264_preset}",
        f"--hardware-acceleration={config.hardware_acceleration}",
        f"--audio-codec={config.chunk_audio_codec}",
        f"--audio-bitrate={config.chunk_audio_bitrate}",
        f"--sample-rate={config.sample_rate}",
        f"--frames={chunk.start}-{chunk.end}",
        f'--props={{"renderFps":{config.fps}}}',
        f"--concurrency={config.render_concurrency}",
        "--enforce-audio-track",
        "--disallow-parallel-encoding",
        "--overwrite",
    ]


def tail_file(path: Path, *, lines: int = 40) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(content[-lines:])


def render_chunk(
    config: RenderConfig,
    bundle_root: Path,
    chunk: Chunk,
    signature: str,
    cancellation_event: threading.Event | None = None,
) -> None:
    paths = chunk_paths(config, chunk)
    paths.media.parent.mkdir(parents=True, exist_ok=True)
    paths.log.parent.mkdir(parents=True, exist_ok=True)
    paths.partial_media.unlink(missing_ok=True)
    atomic_write_text(paths.partial_signature, signature + os.linesep)
    print(f"render chunk {chunk.index}: frames={chunk.start}-{chunk.end}")
    try:
        run_command(
            render_command(config, bundle_root, chunk, paths.partial_media),
            cwd=config.video_root,
            log_path=paths.log,
            cancellation_event=cancellation_event,
        )
    except subprocess.CalledProcessError as exc:
        raise RenderError(f"chunk {chunk.index} render failed; see {paths.log}\n{tail_file(paths.log)}") from exc

    if not media_is_valid(
        config,
        paths.partial_media,
        expected_frames=chunk.frame_count,
        expected_audio_codec=config.chunk_audio_codec,
        label="chunk",
    ):
        raise RenderError(f"chunk {chunk.index} failed post-render media validation; see {paths.log}")
    os.replace(paths.partial_media, paths.media)
    atomic_write_text(paths.signature, signature + os.linesep)
    paths.partial_signature.unlink(missing_ok=True)
    print(f"complete chunk {chunk.index}: frames={chunk.frame_count} signature=written")


def render_pending_chunks(
    config: RenderConfig,
    bundle_root: Path,
    pending: Sequence[Chunk],
    signature: str,
) -> None:
    if not pending:
        return
    if config.jobs == 1:
        for chunk in pending:
            render_chunk(config, bundle_root, chunk, signature)
        return

    print(
        f"parallel chunk rendering: outer_jobs={config.jobs} "
        f"inner_concurrency={config.render_concurrency} total_workers={config.total_render_workers}"
    )
    first_error: BaseException | None = None
    cancellation_event = threading.Event()
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=config.jobs,
        thread_name_prefix="remotion-chunk",
    )
    futures: dict[concurrent.futures.Future[None], Chunk] = {}
    try:
        futures = {
            executor.submit(
                render_chunk,
                config,
                bundle_root,
                chunk,
                signature,
                cancellation_event,
            ): chunk
            for chunk in pending
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except BaseException as exc:  # preserve SystemExit/interrupt semantics from workers
                first_error = exc
                cancellation_event.set()
                for candidate in futures:
                    candidate.cancel()
                terminate_active_processes()
                break
    except BaseException:
        cancellation_event.set()
        for candidate in futures:
            candidate.cancel()
        terminate_active_processes()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    if first_error is not None:
        raise first_error


def ffconcat_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def probe_audio_duration_samples(path: Path) -> tuple[str, int]:
    try:
        result = run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=time_base,duration_ts",
                "-of",
                "json",
                str(path),
            ]
        )
    except subprocess.CalledProcessError as exc:
        raise RenderError(f"unable to probe audio alignment: {path}") from exc
    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
        return str(stream["time_base"]), int(stream["duration_ts"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise RenderError(f"audio alignment metadata is missing: {path}") from exc


def final_audio_alignment_is_valid(config: RenderConfig, path: Path, total_frames: int) -> bool:
    numerator = total_frames * config.sample_rate
    if numerator % config.fps:
        raise RenderError(
            f"non-integral final audio sample count: frames={total_frames} "
            f"fps={config.fps} sample_rate={config.sample_rate}"
        )
    expected_samples = numerator // config.fps
    time_base, actual_samples = probe_audio_duration_samples(path)
    if time_base != f"1/{config.sample_rate}":
        print(
            f"unexpected final audio time base: expected=1/{config.sample_rate} actual={time_base}",
            file=sys.stderr,
        )
        return False
    tolerance = config.sample_rate // 1000
    difference = abs(actual_samples - expected_samples)
    print(
        f"final audio alignment: expected_samples={expected_samples} actual_samples={actual_samples} "
        f"delta={difference} tolerance={tolerance}"
    )
    return difference <= tolerance


def full_decode(path: Path) -> None:
    try:
        run_command(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"])
    except subprocess.CalledProcessError as exc:
        raise RenderError(f"full decode failed for {path}:\n{command_error(exc)}") from exc


def final_signature_path(config: RenderConfig) -> Path:
    return config.output_path.with_name(config.output_path.name + ".render-signature")


def existing_final_is_valid(config: RenderConfig, total_frames: int, signature: str) -> bool:
    if not signature_matches(final_signature_path(config), signature):
        return False
    if not media_is_valid(
        config,
        config.output_path,
        expected_frames=total_frames,
        expected_audio_codec=config.final_audio_codec,
        label="final",
    ):
        return False
    if not final_audio_alignment_is_valid(config, config.output_path, total_frames):
        return False
    full_decode(config.output_path)
    print(f"skip final assembly: validated signature match at {config.output_path}")
    return True


def cleanup_assembly(config: RenderConfig) -> None:
    if config.keep_assembly:
        print(f"assembly workspace kept at: {config.assembly_dir}")
        return
    try:
        shutil.rmtree(config.assembly_dir)
    except FileNotFoundError:
        return
    except OSError as exc:
        print(
            f"warning: unable to remove assembly workspace {config.assembly_dir}: {exc}",
            file=sys.stderr,
        )


def assemble_final(
    config: RenderConfig,
    chunks: Sequence[Chunk],
    total_frames: int,
    signature: str,
) -> None:
    if existing_final_is_valid(config, total_frames, signature):
        cleanup_assembly(config)
        return

    work = config.assembly_dir
    pcm_dir = work / "audio-pcm"
    work.mkdir(parents=True, exist_ok=True)
    pcm_dir.mkdir(parents=True, exist_ok=True)
    video_list = work / "video.ffconcat"
    audio_list = work / "audio.ffconcat"
    video_lines = ["ffconcat version 1.0"]
    audio_lines = ["ffconcat version 1.0"]

    for chunk in chunks:
        paths = chunk_paths(config, chunk)
        if not media_is_valid(
            config,
            paths.media,
            expected_frames=chunk.frame_count,
            expected_audio_codec=config.chunk_audio_codec,
            label="chunk",
        ):
            raise RenderError(f"chunk media specification mismatch: {paths.media}")
        if not signature_matches(paths.signature, signature):
            raise RenderError(f"chunk render signature missing or stale: {paths.signature}")

        numerator = chunk.frame_count * config.sample_rate
        if numerator % config.fps:
            raise RenderError(f"non-integral audio sample count for chunk {chunk.index}")
        samples = numerator // config.fps
        seconds = chunk.frame_count / config.fps
        wav = pcm_dir / f"chunk-{chunk.index:03d}.wav"
        try:
            run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "warning",
                    "-i",
                    str(paths.media),
                    "-vn",
                    "-af",
                    (
                        f"aresample={config.sample_rate}:async=0:first_pts=0,"
                        f"aformat=sample_rates={config.sample_rate}:channel_layouts=stereo,"
                        f"atrim=start_sample=0:end_sample={samples},apad=whole_len={samples},"
                        f"atrim=start_sample=0:end_sample={samples},asetpts=N/SR/TB"
                    ),
                    "-c:a",
                    "pcm_s16le",
                    "-ar",
                    str(config.sample_rate),
                    "-ac",
                    "2",
                    str(wav),
                ]
            )
        except subprocess.CalledProcessError as exc:
            raise RenderError(f"failed to extract exact PCM audio from {paths.media}") from exc
        time_base, actual_samples = probe_audio_duration_samples(wav)
        if time_base != f"1/{config.sample_rate}" or actual_samples != samples:
            raise RenderError(
                f"sample mismatch: {wav} expected={samples} actual={actual_samples} time_base={time_base}"
            )
        video_lines.extend((f"file {ffconcat_quote(paths.media)}", f"duration {seconds:.9f}"))
        audio_lines.append(f"file {ffconcat_quote(wav)}")

    atomic_write_text(video_list, "\n".join(video_lines) + "\n")
    atomic_write_text(audio_list, "\n".join(audio_lines) + "\n")
    video_only = work / "video-only.mp4"
    audio_only = work / "audio.m4a"
    partial_final = work / f"{config.output_slug}.partial.mp4"
    total_seconds = total_frames / config.fps
    commands = [
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(video_list),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-an",
            "-movflags",
            "+faststart",
            str(video_only),
        ],
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(audio_list),
            "-map",
            "0:a:0",
            "-c:a",
            "aac",
            "-b:a",
            config.final_audio_bitrate,
            "-ar",
            str(config.sample_rate),
            "-ac",
            "2",
            "-cutoff",
            "18000",
            str(audio_only),
        ],
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-i",
            str(video_only),
            "-i",
            str(audio_only),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-t",
            f"{total_seconds:.9f}",
            "-movflags",
            "+faststart",
            str(partial_final),
        ],
    ]
    partial_final.unlink(missing_ok=True)
    for command in commands:
        try:
            run_command(command)
        except subprocess.CalledProcessError as exc:
            raise RenderError(command_error(exc)) from exc

    full_decode(partial_final)
    if not media_is_valid(
        config,
        partial_final,
        expected_frames=total_frames,
        expected_audio_codec=config.final_audio_codec,
        label="final",
    ):
        raise RenderError(f"final media specification mismatch: {partial_final}")
    if not final_audio_alignment_is_valid(config, partial_final, total_frames):
        raise RenderError(f"final audio alignment mismatch: {partial_final}")

    publish_file_atomically(partial_final, config.output_path)
    atomic_write_text(final_signature_path(config), signature + os.linesep)
    print(f"final: {config.output_path}")
    cleanup_assembly(config)


def calculate_total_frames(config: RenderConfig, timeline: TimelineInfo) -> int:
    return math.floor(timeline.last_end_ms * config.fps / 1000) + config.intro_frames


def render_pipeline(config: RenderConfig) -> None:
    validate_required_tools()
    with AtomicRenderLock(config.lock_dir, project=config.project, composition=config.composition):
        timeline = validate_timeline(config)
        print(
            f"timeline preflight: items={timeline.item_count} size={config.width}x{config.height} "
            f"unique_images={len(set(timeline.image_names))}"
        )

        before = get_memory_free_percent()
        print(f"memory before TTS shutdown: free={before}%")
        stop_selected_tts(config)
        after = get_memory_free_percent()
        required = config.required_memory_free_percent
        print(
            f"memory after TTS shutdown: free={after}% required={required}% "
            f"outer_jobs={config.jobs} inner_concurrency={config.render_concurrency}"
        )
        if after < required:
            mode = "serial" if config.total_render_workers == 1 else "parallel"
            raise RenderError(
                f"memory pressure gate blocked {mode} 4K60 render: free={after}% required={required}%"
            )
        check_scratch_space(config)

        total_frames = calculate_total_frames(config, timeline)
        chunks = build_chunks(total_frames, config.chunk_frames)
        signature = build_render_signature(config, timeline)
        config.chunk_dir.mkdir(parents=True, exist_ok=True)
        config.log_dir.mkdir(parents=True, exist_ok=True)
        pending = [
            chunk
            for chunk in chunks
            if prepare_chunk(config, chunk, signature)
        ]

        if pending:
            with minimal_bundle_workspace(config) as (public_root, bundle_root):
                build_bundle(config, public_root, bundle_root)
                render_pending_chunks(config, bundle_root, pending, signature)
        else:
            print("all chunks passed media/signature validation; Remotion bundle not needed")

        assemble_final(config, chunks, total_frames, signature)
        print(f"composition: {config.composition}")
        print(f"items: {timeline.item_count}")
        print(f"frames: {total_frames}")
        print(f"chunks: {len(chunks)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = config_from_args(args)
        with signal_cleanup():
            render_pipeline(config)
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("error: render interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
