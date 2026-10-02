import argparse
import copy
from pathlib import Path

import pytest

from scripts import video_workflow as workflow
from scripts.workflow_io import lines, read, sha256, write


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "ROOT", tmp_path)
    script = tmp_path / "story_script.json"
    for name in ("a", "b"):
        (tmp_path / f"{name}.wav").write_bytes(name.encode())
    write(script, {"projectId": "Demo", "lines": [
        {"id": "a", "speakerId": "A", "audio": str(tmp_path / "a.wav"), "spokenText": "こんにちは", "subtitleZh": "你好"},
        {"id": "b", "speakerId": "B", "audio": str(tmp_path / "b.wav"), "spokenText": "また明日", "subtitleZh": "明天见"}]})
    write(tmp_path / "my-video/public/content/Demo/timeline.json", {
        "text": [{"id": "a", "startMs": 0, "endMs": 900}, {"id": "b", "startMs": 1000, "endMs": 2000}],
        "elements": [{"startMs": 0, "endMs": 2100}]})
    return script


def args(**kwargs):
    return argparse.Namespace(project=None, line_id=None, speaker_id=None, text=None, at=None, intro_ms=1000, **kwargs)


@pytest.mark.parametrize("stamp, expected, gap", [("0.5", [], False), ("1", ["a"], False),
    ("1.95", ["a"], True), ("00:02", ["b"], False), ("3.1", [], False)])
def test_locate_intro_and_gap(project, stamp, expected, gap):
    query = args()
    query.at = stamp
    found = workflow.locate(project, query)["lines"]
    assert [x["id"] for x in found] == expected
    if found:
        assert found[0]["inGap"] == gap


def test_locate_text_and_unknown_id(project):
    query = args()
    query.text = "明天"
    assert [x["id"] for x in workflow.locate(project, query)["lines"]] == ["b"]
    query.line_id = ["typo"]
    with pytest.raises(ValueError, match="Unknown"):
        workflow.locate(project, query)


def test_locate_qc_hash_must_match(project):
    write(project.parent / "story_audio_qc/qc_report.json", {"lines": [{"id": "a", "sha256": "wrong", "status": "pass"}]})
    assert workflow.locate(project, args())["lines"][0]["qcMatchesAudio"] is False


def create_repair(project, patch=None):
    query = args(script=str(project), work_dir=str(project.parent / "repair"), patch=patch)
    query.line_id = ["a"]
    workflow.repair_create(query)
    return Path(query.work_dir)


def test_repair_isolated_and_idempotent_safety(project):
    original = project.read_bytes()
    patch = project.parent / "patch.json"
    write(patch, {"a": {"spokenText": "新しい声"}})
    work = create_repair(project, str(patch))
    assert project.read_bytes() == original
    assert read(work / "candidate_script.json")["lines"][0]["spokenText"] == "新しい声"
    assert read(work / "candidate_script.json")["lines"][0]["audio"] == str(work / "candidates/a.wav")
    assert (project.parent / "a.wav").read_bytes() == b"a"
    with pytest.raises(ValueError, match="already exists"):
        create_repair(project)


def test_repair_refuses_source_change(project):
    work = create_repair(project)
    script = read(project)
    script["title"] = "changed"
    write(project, script)
    with pytest.raises(ValueError, match="inputs changed"):
        workflow.repair_stage(argparse.Namespace(action="compare", work_dir=str(work)))


def test_repair_refuses_audio_change(project):
    work = create_repair(project)
    (project.parent / "a.wav").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Original WAV changed"):
        workflow.repair_stage(argparse.Namespace(action="compare", work_dir=str(work)))


def test_repair_only_applied_lines_update_script(project, monkeypatch):
    patch = project.parent / "patch.json"
    write(patch, {"a": {"spokenText": "new"}})
    work = create_repair(project, str(patch))
    before_b = copy.deepcopy(read(project)["lines"][1])
    candidate = work / "candidates/a.wav"
    candidate.parent.mkdir()
    candidate.write_bytes(b"candidate")
    def fake_compare(_):
        (project.parent / "a.wav").write_bytes(candidate.read_bytes())
        write(work / "candidates/comparison_report.json", {"comparisons": [
            {"id": "a", "applied": True, "candidate": {"sha256": sha256(candidate)}}]})
    monkeypatch.setattr(workflow, "run", fake_compare)
    workflow.repair_stage(argparse.Namespace(action="apply", work_dir=str(work)))
    assert read(project)["lines"][0]["spokenText"] == "new"
    assert read(project)["lines"][1] == before_b
    assert (project.parent / "b.wav").read_bytes() == b"b"
    assert read(work / "source_before_apply.json")["lines"][0]["spokenText"] != "new"


def test_failed_candidate_never_changes_script(project, monkeypatch):
    work = create_repair(project)
    before = project.read_bytes()
    def fake_compare(_):
        write(work / "candidates/comparison_report.json", {"comparisons": [{"id": "a", "applied": False}]})
    monkeypatch.setattr(workflow, "run", fake_compare)
    workflow.repair_stage(argparse.Namespace(action="apply", work_dir=str(work)))
    assert project.read_bytes() == before


def test_duplicate_ids_and_output_paths_rejected(project):
    script = read(project)
    script["lines"][1]["id"] = "a"
    with pytest.raises(ValueError, match="stable id"):
        lines(script)
    script["lines"][1]["id"] = "b"
    script["lines"][1]["audio"] = script["lines"][0]["audio"]
    with pytest.raises(ValueError, match="share"):
        lines(script)


def test_nested_scene_lines_are_mutable():
    script = {"scenes": [{"lines": [{"id": "a"}]}]}
    lines(script)[0]["subtitleZh"] = "改了"
    assert script["scenes"][0]["lines"][0]["subtitleZh"] == "改了"


def test_pipeline_does_not_allow_overwrite_or_backend_override():
    config = {"script": "x", "tts": {"engine": "fishspeech", "url": "http://127.0.0.1:8002", "modelId": "s2", "args": ["--overwrite"]}}
    with pytest.raises(ValueError, match="override"):
        workflow.tts_options(config)


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "1:60", "1:2:3:4"])
def test_invalid_timestamp(value):
    with pytest.raises(ValueError):
        workflow.seconds(value)


@pytest.fixture
def pipeline(project, monkeypatch):
    config_path = project.parent / "story.workflow.json"
    config = {"script": str(project), "tts": {"engine": "fishspeech", "url": "http://127.0.0.1:8002", "modelId": "s2-v1"}}
    write(config_path, config)
    keys = {"audio": "audio-v1", "visual": "visual-v1"}
    monkeypatch.setattr(workflow, "audio_key", lambda _: keys["audio"])
    monkeypatch.setattr(workflow, "visual_key", lambda _: keys["visual"])
    commands = []
    monkeypatch.setattr(workflow, "run", lambda cmd, **kw: commands.append(cmd))
    def qc(cmd, **kw):
        commands.append(cmd)
        write(Path(cmd[cmd.index("--report") + 1]), {"summary": {"error": 0, "review": 0, "roletoneErrors": 0}})
        return argparse.Namespace(returncode=0)
    monkeypatch.setattr(workflow.subprocess, "run", qc)
    monkeypatch.setattr(workflow, "subtitle_check", lambda script, output, project: write(output, {"flagged": []}))
    def preview(script, output, ids, project):
        write(output / "preview.json", {"stills": ids})
        (output / "a.png").write_bytes(b"still")
    monkeypatch.setattr(workflow, "preview", preview)
    return config_path, keys, commands


def pipeline_args(config, command="run", **kwargs):
    return argparse.Namespace(config=str(config), command=command, until="render", accept_warnings=False, **kwargs)


def test_pipeline_review_gates_and_final_wrapper_only(pipeline):
    config, keys, commands = pipeline
    workflow.workflow(pipeline_args(config))
    assert not any("director.py" in " ".join(c) for c in commands)
    workflow.workflow(pipeline_args(config, "approve", stage="audio"))
    workflow.workflow(pipeline_args(config))
    assert any("director.py" in " ".join(c) and "100" in c for c in commands)
    assert not any("render_4k60.sh" in " ".join(c) for c in commands)
    workflow.workflow(pipeline_args(config, "approve", stage="visual"))
    workflow.workflow(pipeline_args(config))
    final = commands[-1]
    assert final[0].endswith("render_4k60.sh")
    assert final[-2:] == ["--tts-url", "http://127.0.0.1:8002"]
    assert sum("audio_qc.py" in " ".join(c) for c in commands) == 1


def test_pipeline_changes_revoke_approval(pipeline):
    config, keys, _ = pipeline
    workflow.workflow(pipeline_args(config))
    keys["audio"] = "new audio"
    with pytest.raises(ValueError, match="changed"):
        workflow.workflow(pipeline_args(config, "approve", stage="audio"))


def test_pipeline_changed_preview_cannot_be_approved(pipeline):
    config, _, _ = pipeline
    workflow.workflow(pipeline_args(config))
    workflow.workflow(pipeline_args(config, "approve", stage="audio"))
    workflow.workflow(pipeline_args(config))
    (config.with_suffix(".work") / "stills/a.png").write_bytes(b"changed preview")
    with pytest.raises(ValueError, match="previews changed"):
        workflow.workflow(pipeline_args(config, "approve", stage="visual"))


def test_pipeline_failure_cannot_reuse_old_qc_report(pipeline, monkeypatch):
    config, keys, _ = pipeline
    workflow.workflow(pipeline_args(config))
    keys["audio"] = "v2"
    monkeypatch.setattr(workflow.subprocess, "run", lambda *a, **kw: argparse.Namespace(returncode=1))
    with pytest.raises(ValueError, match="new report"):
        workflow.workflow(pipeline_args(config))


def test_repair_parameter_override_is_reusable_under_production_settings(project, monkeypatch):
    from scripts.synthesize_script import build_parser as tts_parser
    from scripts.tts_manifest import cache_status, record, request_fingerprint
    character = project.parent / "characters/A"
    character.mkdir(parents=True)
    (character / "reference.wav").write_bytes(b"reference")
    (character / "reference_jp.txt").write_text("reference words")
    production = tts_parser().parse_args(["--script", str(project), "--tts-engine", "fishspeech",
        "--model-id", "s2-v1", "--characters-root", str(character.parent), "--temperature", "0.7"])
    original = read(project)["lines"][0]
    record(Path(original["audio"]), original, request_fingerprint(original, production),
           endpoint="local", settings=vars(production))
    patch = project.parent / "patch.json"
    write(patch, {"a": {"spokenText": "changed sentence"}})
    work = create_repair(project, str(patch))
    candidate_line = read(work / "candidate_script.json")["lines"][0]
    candidate = Path(candidate_line["audio"])
    candidate.parent.mkdir()
    candidate.write_bytes(b"new voice")
    retry = copy.deepcopy(production)
    retry.temperature = 0.2
    record(candidate, candidate_line, request_fingerprint(candidate_line, retry), endpoint="local", settings=vars(retry))
    def apply(_):
        Path(original["audio"]).write_bytes(candidate.read_bytes())
        write(work / "candidates/comparison_report.json", {"comparisons": [
            {"id": "a", "applied": True, "candidate": {"sha256": sha256(candidate)}}]})
    monkeypatch.setattr(workflow, "run", apply)
    workflow.repair_stage(argparse.Namespace(action="apply", work_dir=str(work)))
    new_line = read(project)["lines"][0]
    assert cache_status(Path(new_line["audio"]), request_fingerprint(new_line, production)) == "accepted"
    (character / "reference.wav").write_bytes(b"another reference")
    assert cache_status(Path(new_line["audio"]), request_fingerprint(new_line, production)) == "stale"
