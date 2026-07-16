from __future__ import annotations

import errno
import json
import signal
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import render_4k60 as renderer


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def make_config(
    tmp_path: Path,
    *,
    jobs: int = 1,
    render_concurrency: int = 1,
) -> renderer.RenderConfig:
    repo = tmp_path / "repo"
    video = repo / "my-video"
    project = "Test_Project_4k60"
    project_root = video / "public" / "content" / project
    (project_root / "images").mkdir(parents=True, exist_ok=True)
    (project_root / "audio").mkdir(parents=True, exist_ok=True)
    (video / "out").mkdir(parents=True, exist_ok=True)
    (video / "src").mkdir(parents=True, exist_ok=True)
    (video / "src" / "index.ts").write_text("export {};\n", encoding="utf-8")
    for name in ("remotion.config.ts", "package.json", "pnpm-lock.yaml", "tsconfig.json"):
        (video / name).write_text(f"{name}\n", encoding="utf-8")
    return renderer.RenderConfig(
        repo_root=repo,
        project=project,
        composition="Test-Project-4k60",
        output_slug="test-project-4k60",
        output_path=video / "out" / "test-project-4k60.mp4",
        tts_engine="fishspeech",
        tts_url="http://127.0.0.1:8002",
        jobs=jobs,
        render_concurrency=render_concurrency,
        lock_dir=tmp_path / "render.lock",
        temp_root=tmp_path / "temp",
    )


def write_shared_image_timeline(config: renderer.RenderConfig) -> None:
    timeline = {
        "width": 3840,
        "height": 2160,
        "elements": [
            {"startMs": 0, "endMs": 1100, "imageUrl": "shared"},
            {"startMs": 1100, "endMs": 2300, "imageUrl": "shared"},
        ],
        "text": [
            {"id": "line001", "startMs": 0, "endMs": 1000},
            {"id": "line002", "startMs": 1100, "endMs": 2200},
        ],
        "audio": [
            {"audioUrl": "line001", "startMs": 0, "endMs": 1000},
            {"audioUrl": "line002", "startMs": 1100, "endMs": 2200},
        ],
    }
    write_json(config.timeline_path, timeline)
    (config.project_root / "images" / "shared.png").write_bytes(b"png")
    (config.project_root / "audio" / "line001.mp3").write_bytes(b"mp3-1")
    (config.project_root / "audio" / "line002.mp3").write_bytes(b"mp3-2")


def test_timeline_accepts_shared_prepared_image(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_shared_image_timeline(config)

    info = renderer.validate_timeline(config)

    assert info.item_count == 2
    assert info.image_names == ("shared", "shared")
    assert len(set(info.image_names)) == 1
    assert info.audio_names == ("line001", "line002")


def test_timeline_still_requires_every_referenced_image(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_shared_image_timeline(config)
    (config.project_root / "images" / "shared.png").unlink()

    with pytest.raises(renderer.RenderError, match="missing timeline image"):
        renderer.validate_timeline(config)


def test_timeline_accepts_sparse_subtitle_and_audio_arrays(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_json(
        config.timeline_path,
        {
            "width": 3840,
            "height": 2160,
            "elements": [
                {"startMs": 0, "endMs": 1000, "imageUrl": "shared"},
                {"startMs": 1000, "endMs": 2200, "imageUrl": "shared"},
            ],
            "text": [{"id": "subtitle-only", "startMs": 0, "endMs": 900}],
            "audio": [{"audioUrl": "audio-only", "startMs": 1000, "endMs": 2100}],
        },
    )
    (config.project_root / "images" / "shared.png").write_bytes(b"png")
    (config.project_root / "audio" / "audio-only.mp3").write_bytes(b"mp3")

    info = renderer.validate_timeline(config)

    assert info.item_count == 2
    assert info.audio_names == ("audio-only",)
    assert info.last_end_ms == 2200


def test_timeline_requires_matching_ids_only_when_text_and_audio_share_a_start(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    write_shared_image_timeline(config)
    timeline = json.loads(config.timeline_path.read_text(encoding="utf-8"))
    timeline["audio"][0]["audioUrl"] = "different"
    write_json(config.timeline_path, timeline)
    (config.project_root / "audio" / "different.mp3").write_bytes(b"mp3")

    with pytest.raises(renderer.RenderError, match="subtitle id and audio id differ at 0ms"):
        renderer.validate_timeline(config)


def test_timeline_accepts_legacy_subtitle_without_id(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_shared_image_timeline(config)
    timeline = json.loads(config.timeline_path.read_text(encoding="utf-8"))
    del timeline["text"][0]["id"]
    timeline["audio"][0]["audioUrl"] = "legacy-audio"
    write_json(config.timeline_path, timeline)
    (config.project_root / "audio" / "legacy-audio.mp3").write_bytes(b"mp3")

    info = renderer.validate_timeline(config)

    assert info.item_count == 2
    assert info.audio_names == ("legacy-audio", "line002")


def test_render_signature_is_independent_of_scheduling(tmp_path: Path) -> None:
    serial = make_config(tmp_path, jobs=1, render_concurrency=1)
    write_shared_image_timeline(serial)
    timeline = renderer.validate_timeline(serial)

    outer_parallel = replace(serial, jobs=2)
    inner_parallel = replace(serial, render_concurrency=2)

    assert renderer.build_render_signature(serial, timeline) == renderer.build_render_signature(
        outer_parallel, timeline
    )
    assert renderer.build_render_signature(serial, timeline) == renderer.build_render_signature(
        inner_parallel, timeline
    )


def test_parallel_workers_raise_memory_and_scratch_gates(tmp_path: Path) -> None:
    serial = make_config(tmp_path)
    two_outer = make_config(tmp_path, jobs=2)
    four_total = make_config(tmp_path, jobs=2, render_concurrency=2)

    assert serial.required_memory_free_percent == 40
    assert two_outer.required_memory_free_percent == 50
    assert four_total.required_memory_free_percent == 70
    assert serial.required_scratch_gib == 20
    assert two_outer.required_scratch_gib == 40


def test_config_rejects_unbounded_parallelism(tmp_path: Path) -> None:
    config = make_config(tmp_path, jobs=3, render_concurrency=2)

    with pytest.raises(renderer.RenderError, match="jobs × render-concurrency"):
        renderer.validate_config(config)


def test_minimal_bundle_workspace_contains_only_target_project(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    with renderer.minimal_bundle_workspace(config) as (public_root, bundle_root):
        run_root = public_root.parent
        entries = list((public_root / "content").iterdir())
        assert entries == [public_root / "content" / config.project]
        assert entries[0].is_symlink()
        assert entries[0].resolve() == config.project_root.resolve()
        assert bundle_root.parent == run_root
        command = renderer.bundle_command(config, public_root, bundle_root)
        assert f"--public-dir={public_root}" in command
        assert str(config.video_root / "public") not in command

    assert not run_root.exists()


def test_render_commands_reuse_prebuilt_bundle() -> None:
    config = renderer.RenderConfig(
        repo_root=Path("/repo"),
        project="P",
        composition="P",
        output_slug="p",
        output_path=Path("/repo/my-video/out/p.mp4"),
        tts_engine="fishspeech",
        tts_url="http://127.0.0.1:8002",
    )
    bundle = Path("/private/tmp/minimal/bundle")
    first = renderer.render_command(config, bundle, renderer.Chunk(0, 0, 2999), Path("first.mp4"))
    second = renderer.render_command(config, bundle, renderer.Chunk(1, 3000, 5999), Path("second.mp4"))

    assert first[3:6] == ["render", str(bundle), "P"]
    assert second[3:6] == ["render", str(bundle), "P"]
    assert not any(argument.startswith("--public-dir") for argument in first + second)
    assert "--enforce-audio-track" in first


def media_probe_payload(*, audio_samples: int, time_base: str = "1/48000") -> dict[str, object]:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "codec_tag_string": "avc1",
                "width": 3840,
                "height": 2160,
                "r_frame_rate": "60/1",
                "avg_frame_rate": "60/1",
                "pix_fmt": "yuvj420p",
                "nb_read_frames": "3000",
                "tags": {"encoder": "Lavc libx264"},
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "time_base": time_base,
                "duration_ts": str(audio_samples),
            },
        ]
    }


def test_chunk_media_accepts_normal_aac_padding(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    media = tmp_path / "chunk.mp4"
    media.write_bytes(b"media")
    monkeypatch.setattr(
        renderer,
        "probe_media",
        lambda _path: media_probe_payload(audio_samples=2_400_000 + 2304),
    )

    assert renderer.media_is_valid(
        config,
        media,
        expected_frames=3000,
        expected_audio_codec="aac",
        label="chunk",
    )


def test_chunk_media_rejects_truncated_aac(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    media = tmp_path / "chunk.mp4"
    media.write_bytes(b"media")
    monkeypatch.setattr(
        renderer,
        "probe_media",
        lambda _path: media_probe_payload(audio_samples=1),
    )

    assert not renderer.media_is_valid(
        config,
        media,
        expected_frames=3000,
        expected_audio_codec="aac",
        label="chunk",
    )


def test_chunk_resume_requires_matching_signature(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    chunk = renderer.Chunk(0, 0, 9)
    paths = renderer.chunk_paths(config, chunk)
    paths.media.parent.mkdir(parents=True)
    paths.media.write_bytes(b"valid-media")
    paths.signature.write_text("old-signature\n", encoding="utf-8")
    validation_calls = 0

    def validator(*_args, **_kwargs):
        nonlocal validation_calls
        validation_calls += 1
        return True

    assert renderer.prepare_chunk(config, chunk, "new-signature", validator=validator) is True
    assert validation_calls == 0, "stale chunks need no expensive full media probe"
    paths.signature.write_text("new-signature\n", encoding="utf-8")
    assert renderer.prepare_chunk(config, chunk, "new-signature", validator=validator) is False
    assert validation_calls == 1


def test_partial_chunk_recovery_also_requires_matching_signature(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    chunk = renderer.Chunk(0, 0, 9)
    paths = renderer.chunk_paths(config, chunk)
    paths.partial_media.parent.mkdir(parents=True)
    paths.partial_media.write_bytes(b"partial")
    paths.partial_signature.write_text("current\n", encoding="utf-8")

    needs_render = renderer.prepare_chunk(
        config,
        chunk,
        "current",
        validator=lambda *_args, **_kwargs: True,
    )

    assert needs_render is False
    assert paths.media.read_bytes() == b"partial"
    assert paths.signature.read_text(encoding="utf-8").strip() == "current"
    assert not paths.partial_signature.exists()


def test_atomic_render_lock_refuses_second_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "render.lock"
    with renderer.AtomicRenderLock(lock_path, project="one", composition="One"):
        with pytest.raises(renderer.RenderError, match="another 4K render"):
            with renderer.AtomicRenderLock(lock_path, project="two", composition="Two"):
                pass

    assert not lock_path.exists()


def test_atomic_render_lock_cleans_up_if_owner_record_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "render.lock"
    monkeypatch.setattr(
        renderer,
        "atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk error")),
    )

    with pytest.raises(OSError, match="disk error"):
        with renderer.AtomicRenderLock(lock_path, project="one", composition="One"):
            pass

    assert not lock_path.exists()


def test_selected_tts_is_health_checked_shutdown_and_listener_confirmed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    events: list[object] = []

    class FakeClient:
        def health(self) -> dict[str, object]:
            events.append("health")
            return {"status": "ok", "loaded": True}

        def shutdown(self, *, wait: bool, wait_timeout: float) -> dict[str, object]:
            events.append(("shutdown", wait, wait_timeout))
            return {"status": "accepted", "server_stopped": True}

        def close(self) -> None:
            events.append("close")

    renderer.stop_selected_tts(
        config,
        client_factory=lambda engine, url, timeout: (
            events.append(("factory", engine, url, timeout)) or FakeClient()
        ),
        listener_checker=lambda port: events.append(("listener", port)),
    )

    assert events == [
        ("factory", "fishspeech", "http://127.0.0.1:8002", 5.0),
        "health",
        ("shutdown", True, 60.0),
        ("listener", 8002),
        "close",
    ]


def test_connection_refused_is_treated_as_already_stopped(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    events: list[object] = []

    class FakeClient:
        def health(self) -> dict[str, object]:
            request = httpx.Request("GET", "http://127.0.0.1:8002/fishspeech/health")
            raise httpx.ConnectError("stopped", request=request)

        def close(self) -> None:
            events.append("close")

    renderer.stop_selected_tts(
        config,
        client_factory=lambda _engine, _url, _timeout: FakeClient(),
        listener_checker=lambda port: events.append(("listener", port)),
    )

    assert events == [("listener", 8002), "close"]


def test_tts_client_protocol_error_is_wrapped_as_render_error(tmp_path: Path) -> None:
    from my_tts.cli import CliError

    config = make_config(tmp_path)
    events: list[str] = []

    class FakeClient:
        def health(self) -> dict[str, object]:
            return {"status": "ok", "loaded": True}

        def shutdown(self, *, wait: bool, wait_timeout: float) -> dict[str, object]:
            raise CliError("invalid shutdown acknowledgement")

        def close(self) -> None:
            events.append("close")

    with pytest.raises(renderer.RenderError, match="shutdown protocol failed"):
        renderer.stop_selected_tts(
            config,
            client_factory=lambda _engine, _url, _timeout: FakeClient(),
            listener_checker=lambda _port: None,
        )

    assert events == ["close"]


def test_scratch_gate_checks_custom_output_parent(tmp_path: Path, monkeypatch) -> None:
    external_output = tmp_path / "external-volume" / "movie.mp4"
    config = replace(make_config(tmp_path), output_path=external_output)
    checked: list[Path] = []

    def fake_disk_usage(path: Path) -> SimpleNamespace:
        checked.append(path)
        return SimpleNamespace(free=100 * 1024**3)

    monkeypatch.setattr(renderer.shutil, "disk_usage", fake_disk_usage)

    renderer.check_scratch_space(config)

    assert external_output.parent in checked


def test_cross_volume_publication_uses_atomic_destination_staging(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "assembly" / "movie.partial.mp4"
    destination = tmp_path / "external" / "movie.mp4"
    source.parent.mkdir()
    source.write_bytes(b"verified-video")
    real_replace = renderer.os.replace

    def fake_replace(old: Path | str, new: Path | str) -> None:
        if Path(old) == source and Path(new) == destination:
            raise OSError(errno.EXDEV, "cross-device link")
        real_replace(old, new)

    monkeypatch.setattr(renderer.os, "replace", fake_replace)

    renderer.publish_file_atomically(source, destination)

    assert destination.read_bytes() == b"verified-video"
    assert not source.exists()
    assert not list(destination.parent.glob(f".{destination.name}.*.tmp"))


def test_successful_assembly_cleanup_respects_keep_flag(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.assembly_dir.mkdir(parents=True)
    (config.assembly_dir / "large-intermediate").write_bytes(b"data")

    renderer.cleanup_assembly(config)

    assert not config.assembly_dir.exists()

    kept = replace(config, keep_assembly=True)
    kept.assembly_dir.mkdir(parents=True)
    renderer.cleanup_assembly(kept)
    assert kept.assembly_dir.exists()


def test_final_audio_alignment_is_bounded_to_one_millisecond(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config(tmp_path)
    expected = 3000 * config.sample_rate // config.fps
    monkeypatch.setattr(
        renderer,
        "probe_audio_duration_samples",
        lambda _path: (f"1/{config.sample_rate}", expected + config.sample_rate // 1000),
    )
    assert renderer.final_audio_alignment_is_valid(config, tmp_path / "final.mp4", 3000)

    monkeypatch.setattr(
        renderer,
        "probe_audio_duration_samples",
        lambda _path: (f"1/{config.sample_rate}", expected + config.sample_rate // 1000 + 1),
    )
    assert not renderer.final_audio_alignment_is_valid(config, tmp_path / "final.mp4", 3000)


def test_run_command_starts_a_new_process_session(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, args, **kwargs) -> None:
            captured["args"] = args
            captured.update(kwargs)

        def communicate(self) -> tuple[str, str]:
            return "out", "err"

        def poll(self) -> int:
            return self.returncode

    monkeypatch.setattr(renderer.subprocess, "Popen", FakeProcess)

    result = renderer.run_command(["tool", "arg"])

    assert result.stdout == "out"
    assert captured["start_new_session"] is True


def test_active_process_cleanup_terminates_then_kills_process_group(monkeypatch) -> None:
    calls: list[tuple[int, signal.Signals]] = []

    class FakeProcess:
        pid = 54321

        def poll(self) -> None:
            return None

    process = FakeProcess()
    monkeypatch.setattr(renderer.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    renderer._register_process(process)  # type: ignore[arg-type]
    try:
        renderer.terminate_active_processes(grace_seconds=0)
    finally:
        renderer._unregister_process(process)  # type: ignore[arg-type]

    assert calls == [(54321, signal.SIGTERM), (54321, signal.SIGKILL)]


def test_parallel_failure_cancels_siblings_and_terminates_active_commands(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config(tmp_path, jobs=2)
    barrier = threading.Barrier(2)
    events: list[str] = []

    def fake_render_chunk(
        _config: renderer.RenderConfig,
        _bundle: Path,
        chunk: renderer.Chunk,
        _signature: str,
        cancellation_event: threading.Event,
    ) -> None:
        barrier.wait(timeout=1)
        if chunk.index == 0:
            raise renderer.RenderError("first chunk failed")
        assert cancellation_event.wait(timeout=1)
        events.append("sibling-cancelled")

    monkeypatch.setattr(renderer, "render_chunk", fake_render_chunk)
    monkeypatch.setattr(
        renderer,
        "terminate_active_processes",
        lambda: events.append("processes-terminated"),
    )

    with pytest.raises(renderer.RenderError, match="first chunk failed"):
        renderer.render_pending_chunks(
            config,
            tmp_path / "bundle",
            [renderer.Chunk(0, 0, 9), renderer.Chunk(1, 10, 19)],
            "signature",
        )

    assert "sibling-cancelled" in events
    assert "processes-terminated" in events


def test_pipeline_bundles_once_for_multiple_pending_chunks(tmp_path: Path, monkeypatch) -> None:
    config = replace(make_config(tmp_path, jobs=2), chunk_frames=60, min_scratch_gib=20)
    write_shared_image_timeline(config)
    timeline = renderer.validate_timeline(config)
    memory_values = iter((75, 75))
    events: list[object] = []

    monkeypatch.setattr(renderer, "validate_required_tools", lambda: None)
    monkeypatch.setattr(renderer, "validate_timeline", lambda _config: timeline)
    monkeypatch.setattr(renderer, "get_memory_free_percent", lambda: next(memory_values))
    monkeypatch.setattr(renderer, "stop_selected_tts", lambda _config: events.append("tts-stopped"))
    monkeypatch.setattr(renderer, "check_scratch_space", lambda _config: None)
    monkeypatch.setattr(renderer, "build_render_signature", lambda _config, _timeline: "signature")
    monkeypatch.setattr(renderer, "prepare_chunk", lambda *_args, **_kwargs: True)

    @contextmanager
    def fake_workspace(_config):
        yield tmp_path / "minimal-public", tmp_path / "bundle"

    monkeypatch.setattr(renderer, "minimal_bundle_workspace", fake_workspace)
    monkeypatch.setattr(
        renderer,
        "build_bundle",
        lambda _config, public, bundle: events.append(("bundle", public, bundle)),
    )
    monkeypatch.setattr(
        renderer,
        "render_pending_chunks",
        lambda _config, bundle, pending, signature: events.append(
            ("render", bundle, len(pending), signature)
        ),
    )
    monkeypatch.setattr(renderer, "assemble_final", lambda *_args: events.append("assembled"))

    renderer.render_pipeline(config)

    bundle_events = [event for event in events if isinstance(event, tuple) and event[0] == "bundle"]
    render_events = [event for event in events if isinstance(event, tuple) and event[0] == "render"]
    assert len(bundle_events) == 1
    assert len(render_events) == 1
    assert render_events[0][2] > 1
    assert events[0] == "tts-stopped"


def test_help_exits_before_any_render_command(monkeypatch) -> None:
    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(renderer, "run_command", fail_if_called)

    with pytest.raises(SystemExit) as exc_info:
        renderer.main(["--help"])

    assert exc_info.value.code == 0
    assert called is False
