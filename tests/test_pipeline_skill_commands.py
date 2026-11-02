"""Keep copyable skill/guide commands compatible with the real CLI contracts."""
import re
import shlex
from pathlib import Path

import pytest

from my_tts.cli import build_parser as client_parser
from scripts.director import build_parser as director_parser
from scripts.render_4k60 import build_parser as render_parser
from scripts.synthesize_script import build_parser as tts_parser
from scripts.tts_manifest import request_fingerprint
from scripts.video_workflow import build_parser as workflow_parser, main, tts_options
from scripts.workflow_io import read, write

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents/skills/umamusume-video-pipeline/SKILL.md"
GUIDE = ROOT / "docs/video-workflow.md"


@pytest.fixture(autouse=True)
def example_environment(monkeypatch, isolated_tts_environment):
    """The copyable commands assume the documented .env has been configured."""
    monkeypatch.setenv("TTS_PROVIDER", "fishspeech")
    monkeypatch.setenv("TTS_API_BASE_URL", "http://127.0.0.1:8002")
    monkeypatch.setenv("TTS_MODEL", "s2-pro-example-v1")
    monkeypatch.setenv("TTS_LANGUAGE", "ja")


def commands(path):
    for block in re.findall(r"```bash\n(.*?)```", path.read_text(), re.S):
        for line in block.replace("\\\n", " ").splitlines():
            tokens = shlex.split(line, comments=True)
            if tokens:
                yield tokens


def parse_command(tokens):
    if tokens[0] == "scripts/render_4k60.sh":
        return render_parser().parse_args(tokens[1:])
    assert tokens[0] == ".venv/bin/python"
    if tokens[1:3] == ["-m", "my_tts.cli"]:
        return client_parser().parse_args(tokens[3:])
    parser = {
        "scripts/video_workflow.py": workflow_parser,
        "scripts/synthesize_script.py": tts_parser,
        "scripts/director.py": director_parser,
    }[tokens[1]]
    args = parser().parse_args(tokens[2:])
    if getattr(args, "command", None) == "repair" and args.action == "generate":
        assert args.tts_args[0] == "--"
        tts_parser().parse_args(["--script", "candidate_script.json", *args.tts_args[1:]])
    return args


@pytest.mark.parametrize("tokens", [*commands(SKILL), *commands(GUIDE)], ids=lambda xs: shlex.join(xs))
def test_documented_command_parses_without_side_effects(tokens):
    parse_command(tokens)


@pytest.mark.parametrize("url,location", [("http://127.0.0.1:8002", "local"),
                                           ("https://gpu.example.invalid", "remote")])
def test_skill_audit_and_retry_match_initialized_generation_profile(tmp_path, monkeypatch, url, location):
    """Following successive examples must not accidentally invalidate a fresh WAV."""
    monkeypatch.setenv("TTS_API_BASE_URL", url)
    script = tmp_path / "script.json"
    line = {"id": "kb001", "speakerId": "hero", "spokenText": "[soft tone]こんにちは。"}
    write(script, {"projectId": "Example_4k60", "lines": [line]})
    config = tmp_path / "example.workflow.json"
    examples = list(commands(SKILL))
    init = next(xs for xs in examples if xs[1:3] == ["scripts/video_workflow.py", "init"])
    init_args = init[2:]
    init_args[init_args.index("--script") + 1] = str(script)
    init_args[init_args.index("--output") + 1] = str(config)
    assert main(init_args) == 0
    assert read(config)["tts"]["url"] == url
    assert read(config)["tts"]["location"] == location
    production = tts_parser().parse_args(tts_options(read(config)))
    audit = next(parse_command(xs) for xs in examples if "--audit" in xs)
    retry_cmd = next(xs for xs in examples if xs[2:4] == ["repair", "generate"])
    retry = tts_parser().parse_args(["--script", str(script), *parse_command(retry_cmd).tts_args[1:]])
    character = tmp_path / "characters/hero"
    character.mkdir(parents=True)
    (character / "reference.wav").write_bytes(b"test reference")
    (character / "reference_jp.txt").write_text("reference transcript")
    for args in (production, audit, retry):
        args.characters_root = str(character.parent)
    assert request_fingerprint(line, production) == request_fingerprint(line, audit)
    assert request_fingerprint(line, production) == request_fingerprint(line, retry)
    assert production.fish_tts_url == audit.fish_tts_url == retry.fish_tts_url == url


def test_workflow_reference_is_resolvable_from_skill():
    assert (SKILL.parent / "../../../docs/video-workflow.md").resolve() == GUIDE
    assert GUIDE.is_file()
