import json

import pytest
from PIL import Image

from scripts import director


@pytest.fixture
def cg_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name, color in (("room", "blue"), ("park", "green"), ("cg", "red")):
        Image.new("RGB", (32, 18), color).save(tmp_path / f"{name}.png")
    catalog = {name: {"source": f"{name}.png"} for name in ("room", "park")}
    director.write_json(tmp_path / "catalog.json", catalog)
    source = {"projectId": "Story", "lines": []}
    for i in range(4):
        (tmp_path / f"l{i}.mp3").write_bytes(f"audio{i}".encode())
        source["lines"].append({"id": f"l{i}", "sceneId": "room_scene", "background": "room",
                                "characters": [], "audio": f"l{i}.mp3", "subtitleZh": f"第{i}句"})
    director.write_json(tmp_path / "script.json", source)
    plan = {"schemaVersion": 1, "projectId": "Story", "shots": [
        {"id": "moment", "startLineId": "l1", "endLineId": "l2", "sceneId": "room_scene",
         "background": "room", "image": "cg.png"}]}
    director.write_json(tmp_path / "cg.json", plan)
    args = director.build_parser().parse_args(["build", "--script", "script.json", "--content-root", "out",
        "--background-catalog", "catalog.json", "--width", "320", "--height", "180", "--line-gap-ms", "100"])
    monkeypatch.setattr(director, "audio_duration_ms", lambda _: 1000)
    return tmp_path, source, catalog, plan, args


def test_cg_is_inclusive_restores_stage_and_keeps_audio_subtitles(cg_project):
    root, source, _, _, args = cg_project
    before = (root / "script.json").read_bytes()
    director.build_project(args)
    project = root / "out/Story"
    baseline = json.loads((project / "timeline.json").read_text())
    audio = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (project / "audio").glob("*.mp3")}
    args.cg_plan = "cg.json"
    director.build_project(args)
    current = json.loads((project / "timeline.json").read_text())
    assert current["audio"] == baseline["audio"]
    assert current["text"] == baseline["text"]
    assert [(x["startMs"], x["endMs"]) for x in current["elements"]] == [(x["startMs"], x["endMs"]) for x in baseline["elements"]]
    urls = [x["imageUrl"] for x in current["elements"]]
    assert urls[0] == urls[3] == baseline["elements"][0]["imageUrl"]
    assert urls[1] == urls[2] != urls[0]
    with Image.open(project / "images" / f"{urls[1]}.png") as image:
        assert image.getpixel((160, 90)) == (255, 0, 0)
    manifest = json.loads((project / ".director-manifest.json").read_text())
    assert manifest["frames"][urls[1]]["inputs"]["sprites"] == []
    assert manifest["lines"]["l1"]["eventCg"]["id"] == "moment"
    assert "eventCg" not in manifest["lines"]["l3"]
    assert (root / "script.json").read_bytes() == before
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (project / "audio").glob("*.mp3")} == audio
    # A changed CG invalidates only its baked visual, not normal shots or audio.
    Image.new("RGB", (32, 18), "yellow").save(root / "cg.png")
    director.build_project(args)
    changed = json.loads((project / "timeline.json").read_text())
    assert changed["elements"][1]["imageUrl"] != urls[1]
    assert changed["elements"][0]["imageUrl"] == urls[0]
    assert changed["audio"] == baseline["audio"]


@pytest.mark.parametrize("mutation, message", [
    (lambda s, p: p.update(projectId="Other"), "projectId"),
    (lambda s, p: p["shots"][0].update(startLineId="missing"), "unknown"),
    (lambda s, p: p["shots"][0].update(startLineId="l3"), "reversed"),
    (lambda s, p: s["lines"][2].update(sceneId="other"), "scene/background mismatch"),
    (lambda s, p: s["lines"][2].update(background="park"), "scene/background mismatch"),
    (lambda s, p: p["shots"].append(dict(p["shots"][0], id="overlap")), "overlapping"),
    (lambda s, p: p["shots"].append(dict(p["shots"][0])), "duplicate CG"),
    (lambda s, p: p["shots"][0].update(image="missing.png"), "not found"),
    (lambda s, p: s["lines"][1].pop("id"), "explicit unique"),
])
def test_invalid_plan_rejected_before_build(cg_project, mutation, message):
    root, source, catalog, plan, _ = cg_project
    mutation(source, plan)
    director.write_json(root / "cg.json", plan)
    with pytest.raises(director.DirectorError, match=message):
        director.load_cg_plan(root / "cg.json", script=source, lines=source["lines"], catalog=catalog, repo_root=root)


def test_nested_scenes_and_inserted_stable_ids(cg_project):
    root, source, catalog, plan, _ = cg_project
    inserted = dict(source["lines"][1], id="inserted")
    source["lines"].insert(2, inserted)
    nested_lines = [{k: v for k, v in line.items() if k not in ("sceneId", "background")} for line in source["lines"]]
    script = {"projectId": "Story", "scenes": [{"id": "room_scene", "background": "room", "lines": nested_lines}]}
    flat = director.flatten_lines(script)
    bound = director.load_cg_plan(root / "cg.json", script=script, lines=flat, catalog=catalog, repo_root=root)
    assert list(bound) == ["l1", "inserted", "l2"]


def test_no_cg_plan_is_backward_compatible(cg_project):
    root, _, _, _, args = cg_project
    director.build_project(args)
    manifest = json.loads((root / "out/Story/.director-manifest.json").read_text())
    assert "cgPlan" not in manifest
    assert all("eventCg" not in x for x in manifest["lines"].values())
