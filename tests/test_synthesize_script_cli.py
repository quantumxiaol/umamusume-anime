from __future__ import annotations

import pytest

from scripts.synthesize_script import build_parser


def test_tts_engine_must_be_selected_explicitly() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--script", "draft/example_script.json"])

    assert exc_info.value.code == 2


@pytest.mark.parametrize("engine", ["fishspeech", "qwen3tts"])
def test_tts_engine_accepts_each_supported_backend(engine: str) -> None:
    args = build_parser().parse_args(
        ["--script", "draft/example_script.json", "--tts-engine", engine]
    )

    assert args.tts_engine == engine
