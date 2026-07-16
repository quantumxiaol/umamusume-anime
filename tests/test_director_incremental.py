from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import director


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def build_args(
    *,
    script_path: Path,
    content_root: Path,
    catalog_path: Path,
    characters_root: Path,
    overwrite: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        script=str(script_path),
        project=None,
        content_root=str(content_root),
        background_catalog=str(catalog_path),
        characters_root=str(characters_root),
        width=320,
        height=180,
        line_gap_ms=0,
        overwrite=overwrite,
        no_auto_focus=False,
    )


def make_visual_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    background_path = tmp_path / "background.png"
    Image.new("RGB", (32, 18), "skyblue").save(background_path)
    catalog_path = tmp_path / "catalog.json"
    write_json(catalog_path, {"terrace": {"source": str(background_path)}})

    characters_root = tmp_path / "characters"
    character_dir = characters_root / "hero"
    character_dir.mkdir(parents=True)
    Image.new("RGBA", (8, 16), (255, 100, 100, 220)).save(character_dir / "ZF_Hero.png")
    write_json(character_dir / "config.json", {"name_zh": "测试角色"})
    return background_path, catalog_path, characters_root


def dialogue_line(line_id: str, *, subtitle_zh: str) -> dict[str, object]:
    return {
        "id": line_id,
        "type": "dialogue",
        "speakerId": "hero",
        "background": "terrace",
        "characters": [{"speakerId": "hero", "slot": "left"}],
        "spokenText": "同じ音声用テキスト",
        "subtitleJa": "同じ日本語字幕",
        "subtitleZh": subtitle_zh,
    }


def test_deduplicates_visuals_and_subtitle_only_change_does_not_recompose(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _, catalog_path, characters_root = make_visual_fixture(tmp_path)
    script_path = tmp_path / "script.json"
    script: dict[str, object] = {
        "projectId": "incremental",
        "title": "增量测试",
        "lines": [
            dialogue_line("l001", subtitle_zh="第一句"),
            dialogue_line("l002", subtitle_zh="第二句"),
        ],
    }
    write_json(script_path, script)
    content_root = tmp_path / "content"

    composed: list[Path] = []
    original_compose = director.compose_resolved_frame

    def counted_compose(*, plan: director.VisualPlan, destination: Path) -> None:
        composed.append(destination)
        original_compose(plan=plan, destination=destination)

    monkeypatch.setattr(director, "compose_resolved_frame", counted_compose)
    args = build_args(
        script_path=script_path,
        content_root=content_root,
        catalog_path=catalog_path,
        characters_root=characters_root,
    )
    director.build_project(args)

    project_root = content_root / "incremental"
    timeline = json.loads((project_root / "timeline.json").read_text(encoding="utf-8"))
    assert len(composed) == 1
    assert timeline["elements"][0]["imageUrl"] == timeline["elements"][1]["imageUrl"]
    image_files = list((project_root / "images").glob("*.png"))
    assert len(image_files) == 1
    assert image_files[0].name.startswith("frame-")
    original_mtime = image_files[0].stat().st_mtime_ns

    manifest = json.loads((project_root / ".director-manifest.json").read_text(encoding="utf-8"))
    image_url = timeline["elements"][0]["imageUrl"]
    inputs = manifest["frames"][image_url]["inputs"]
    assert inputs["canvas"] == {"width": 320, "height": 180}
    assert len(inputs["background"]["sha256"]) == 64
    assert len(inputs["sprites"][0]["sha256"]) == 64
    assert inputs["sprites"][0]["x"] == {"mode": "slot", "value": "left"}

    lines = script["lines"]
    assert isinstance(lines, list)
    lines[0]["subtitleZh"] = "只修改中文字幕"
    write_json(script_path, script)
    director.build_project(args)

    timeline = json.loads((project_root / "timeline.json").read_text(encoding="utf-8"))
    assert len(composed) == 1
    assert image_files[0].stat().st_mtime_ns == original_mtime
    assert timeline["text"][0]["subtitleZh"] == "只修改中文字幕"

    args.overwrite = True
    director.build_project(args)
    assert len(composed) == 2, "--overwrite rebuilds each unique frame once, not once per line"


def test_audio_change_only_rebuilds_affected_audio_and_retimes_following_lines(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _, catalog_path, characters_root = make_visual_fixture(tmp_path)
    source_dir = tmp_path / "source-audio"
    source_dir.mkdir()
    durations = {b"A": 1000, b"B": 1000, b"BB": 2000, b"C": 1000}
    for name, contents in (("a.mp3", b"A"), ("b.mp3", b"B"), ("c.mp3", b"C")):
        (source_dir / name).write_bytes(contents)

    lines: list[dict[str, object]] = []
    for index, name in enumerate(("a.mp3", "b.mp3", "c.mp3"), start=1):
        line = dialogue_line(f"l{index:03d}", subtitle_zh=f"第{index}句")
        line["audio"] = str(source_dir / name)
        lines.append(line)
    script_path = tmp_path / "script.json"
    write_json(script_path, {"projectId": "audio-incremental", "title": "音频测试", "lines": lines})
    content_root = tmp_path / "content"
    args = build_args(
        script_path=script_path,
        content_root=content_root,
        catalog_path=catalog_path,
        characters_root=characters_root,
    )

    prepared: list[str] = []
    original_prepare = director.prepare_audio

    def counted_prepare(**kwargs):
        prepared.append(kwargs["destination"].name)
        return original_prepare(**kwargs)

    monkeypatch.setattr(director, "prepare_audio", counted_prepare)
    monkeypatch.setattr(director, "audio_duration_ms", lambda path: durations[path.read_bytes()])

    director.build_project(args)
    assert prepared == ["l001.mp3", "l002.mp3", "l003.mp3"]
    prepared.clear()

    (source_dir / "b.mp3").write_bytes(b"BB")
    director.build_project(args)
    assert prepared == ["l002.mp3"]

    project_root = content_root / "audio-incremental"
    timeline = json.loads((project_root / "timeline.json").read_text(encoding="utf-8"))
    assert [(item["startMs"], item["endMs"]) for item in timeline["audio"]] == [
        (0, 1000),
        (1000, 3000),
        (3000, 4000),
    ]
    manifest = json.loads((project_root / ".director-manifest.json").read_text(encoding="utf-8"))
    audio_record = manifest["lines"]["l002"]["audio"]
    assert audio_record["inputs"]["transcode"]["mode"] == "copy"
    assert len(audio_record["inputs"]["source"]["sha256"]) == 64


def test_successful_build_prunes_legacy_and_removed_generated_outputs_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _, catalog_path, characters_root = make_visual_fixture(tmp_path)
    second_background = tmp_path / "second-background.png"
    Image.new("RGB", (32, 18), "seagreen").save(second_background)
    write_json(
        catalog_path,
        {
            "terrace": {"source": str(tmp_path / "background.png")},
            "garden": {"source": str(second_background)},
        },
    )
    source_dir = tmp_path / "source-audio"
    source_dir.mkdir()
    (source_dir / "a.mp3").write_bytes(b"A")
    (source_dir / "b.mp3").write_bytes(b"B")

    first = dialogue_line("l001", subtitle_zh="第一句")
    first["audio"] = str(source_dir / "a.mp3")
    second = dialogue_line("l002", subtitle_zh="第二句")
    second["background"] = "garden"
    second["audio"] = str(source_dir / "b.mp3")
    script_path = tmp_path / "script.json"
    payload: dict[str, object] = {
        "projectId": "cleanup",
        "title": "清理测试",
        "lines": [first, second],
    }
    write_json(script_path, payload)
    content_root = tmp_path / "content"
    project_root = content_root / "cleanup"
    (project_root / "images").mkdir(parents=True)
    (project_root / "audio").mkdir()
    (project_root / "images" / "legacy-line.png").write_bytes(b"legacy")
    (project_root / "images" / "orphan.png").write_bytes(b"orphan")
    (project_root / "audio" / "legacy-line.mp3").write_bytes(b"legacy")
    write_json(
        project_root / "timeline.json",
        {
            "elements": [{"imageUrl": "legacy-line"}],
            "audio": [{"audioUrl": "legacy-line"}],
            "text": [],
        },
    )
    (project_root / "images" / "notes.txt").write_text("not a Director image", encoding="utf-8")
    outside = project_root / "outside.png"
    outside.write_bytes(b"outside")

    monkeypatch.setattr(director, "audio_duration_ms", lambda path: 1000)
    args = build_args(
        script_path=script_path,
        content_root=content_root,
        catalog_path=catalog_path,
        characters_root=characters_root,
    )
    director.build_project(args)
    assert not (project_root / "images" / "legacy-line.png").exists()
    assert (project_root / "images" / "orphan.png").exists()
    assert not (project_root / "audio" / "legacy-line.mp3").exists()
    assert (project_root / "images" / "notes.txt").exists()
    assert outside.exists()

    initial_timeline = json.loads((project_root / "timeline.json").read_text(encoding="utf-8"))
    removed_frame = initial_timeline["elements"][1]["imageUrl"]
    lines = payload["lines"]
    assert isinstance(lines, list)
    payload["lines"] = [lines[0]]
    write_json(script_path, payload)
    director.build_project(args)

    assert not (project_root / "images" / f"{removed_frame}.png").exists()
    assert not (project_root / "audio" / "l002.mp3").exists()
    assert (project_root / "audio" / "l001.mp3").exists()
    assert (project_root / "images" / "orphan.png").exists()
    assert outside.exists()
    final_manifest = json.loads(
        (project_root / ".director-manifest.json").read_text(encoding="utf-8")
    )
    assert final_manifest["ownedFiles"]["audio"] == ["l001.mp3"]
    assert f"{removed_frame}.png" not in final_manifest["ownedFiles"]["images"]
