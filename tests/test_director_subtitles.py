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


def test_build_writes_structured_subtitle_with_character_config_label(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    background_path = tmp_path / "background.png"
    Image.new("RGB", (16, 9), "skyblue").save(background_path)

    catalog_path = tmp_path / "catalog.json"
    write_json(catalog_path, {"terrace": {"source": str(background_path)}})
    characters_root = tmp_path / "characters"
    write_json(
        characters_root / "tokai_teio" / "config.json",
        {"id": "uma_tokai_teio", "name_zh": "东海帝王"},
    )
    script_path = tmp_path / "script.json"
    write_json(
        script_path,
        {
            "projectId": "subtitle-test",
            "title": "字幕测试",
            "lines": [
                {
                    "id": "tt001",
                    "type": "dialogue",
                    "speakerId": "uma_tokai_teio",
                    "background": "terrace",
                    "characters": [],
                    "spokenText": "いつもの作戦じゃだめ。",
                    "subtitleJa": "いつもの作戦じゃだめ。",
                    "subtitleZh": "不能再用平时那套了。",
                }
            ],
        },
    )
    content_root = tmp_path / "content"
    args = argparse.Namespace(
        script=str(script_path),
        project=None,
        content_root=str(content_root),
        background_catalog=str(catalog_path),
        characters_root=str(characters_root),
        width=320,
        height=180,
        line_gap_ms=0,
        overwrite=False,
        no_auto_focus=False,
    )

    director.build_project(args)

    timeline = json.loads((content_root / "subtitle-test" / "timeline.json").read_text(encoding="utf-8"))
    assert timeline["text"] == [
        {
            "id": "tt001",
            "startMs": 0,
            "endMs": 3000,
            "kind": "dialogue",
            "position": "bottom",
            "speakerId": "uma_tokai_teio",
            "speakerLabel": "东海帝王",
            "subtitleJa": "いつもの作戦じゃだめ。",
            "subtitleZh": "不能再用平时那套了。",
        }
    ]
    assert "text" not in timeline["text"][0]


def test_explicit_and_default_speaker_labels_take_expected_priority(tmp_path: Path) -> None:
    characters_root = tmp_path / "characters"
    write_json(characters_root / "tokai_teio" / "config.json", {"name_zh": "东海帝王"})

    explicit = director.resolve_speaker_label(
        line={"type": "dialogue", "speakerId": "tokai_teio", "speakerLabel": "帝王"},
        characters_root=characters_root,
    )
    config = director.resolve_speaker_label(
        line={"type": "dialogue", "speakerId": "uma_tokai_teio"},
        characters_root=characters_root,
    )
    trainer = director.resolve_speaker_label(
        line={"type": "dialogue", "speakerId": "trainer"},
        characters_root=characters_root,
    )
    narration = director.resolve_speaker_label(
        line={"type": "narration"},
        characters_root=characters_root,
    )

    assert explicit == "帝王"
    assert config == "东海帝王"
    assert trainer == "训练员"
    assert narration == "旁白"


def test_legacy_subtitle_falls_back_without_flattening_text(tmp_path: Path) -> None:
    item = director.build_subtitle_element(
        line={
            "type": "dialogue",
            "speakerId": "trainer",
            "subtitle": "旧格式字幕",
        },
        line_id="legacy001",
        start_ms=100,
        end_ms=2100,
        characters_root=tmp_path / "characters",
    )

    assert item == {
        "id": "legacy001",
        "startMs": 100,
        "endMs": 2100,
        "kind": "dialogue",
        "position": "bottom",
        "speakerId": "trainer",
        "speakerLabel": "训练员",
        "subtitleJa": "旧格式字幕",
    }
    assert "text" not in item


def test_missing_dialogue_label_and_long_text_warn_without_truncating(tmp_path: Path, capsys) -> None:
    long_text = "長" * 55

    item = director.build_subtitle_element(
        line={"type": "dialogue", "speakerId": "unknown", "subtitleJa": long_text},
        line_id="long001",
        start_ms=0,
        end_ms=3000,
        characters_root=tmp_path / "characters",
    )

    assert item is not None
    assert item["subtitleJa"] == long_text
    assert "speakerLabel" not in item
    warnings = capsys.readouterr().err
    assert "dialogue speaker has no label" in warnings
    assert "subtitleJa is likely too long" in warnings
    assert "text was kept unchanged" in warnings


def test_spoken_text_is_last_subtitle_fallback(tmp_path: Path) -> None:
    item = director.build_subtitle_element(
        line={"type": "narration", "spokenText": "語りの本文"},
        line_id="n001",
        start_ms=0,
        end_ms=3000,
        characters_root=tmp_path / "characters",
    )

    assert item is not None
    assert item["kind"] == "narration"
    assert item["speakerLabel"] == "旁白"
    assert item["subtitleJa"] == "語りの本文"
    assert "subtitleZh" not in item
