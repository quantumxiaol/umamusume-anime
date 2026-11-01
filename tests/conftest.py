"""Never let tests consume a developer's real TTS endpoint or credentials."""
import os

import pytest

from my_tts import config


@pytest.fixture(autouse=True)
def isolated_tts_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    for key in os.environ:
        if key.startswith("TTS_") or key == "FISH_TTS_API_KEY":
            monkeypatch.delenv(key)
