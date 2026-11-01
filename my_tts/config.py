"""TTS defaults from the repository .env, overridden by the process environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_URLS = {
    "fishspeech": "http://127.0.0.1:8002",
    "qwen3tts": "http://127.0.0.1:8001",
    "indextts": "http://127.0.0.1:8000",
}


@dataclass(frozen=True)
class TTSSettings:
    values: dict[str, str] = field(repr=False)

    @property
    def provider(self) -> str | None:
        return self.values.get("TTS_PROVIDER", "").strip().lower() or None

    def for_provider(self, provider: str, key: str) -> str | None:
        if self.provider not in (None, provider):
            return None
        return self.values.get(key, "").strip() or None

    def url(self, provider: str) -> str:
        return self.for_provider(provider, "TTS_API_BASE_URL") or DEFAULT_URLS[provider]

    def api_key(self, provider: str) -> str:
        key = self.for_provider(provider, "TTS_API_KEY")
        if not key and provider == "fishspeech":
            key = self.values.get("FISH_TTS_API_KEY")
        return key or ""

    def model(self, provider: str) -> str | None:
        return self.for_provider(provider, "TTS_MODEL")

    def language(self, provider: str, fallback: str = "Japanese") -> str:
        value = self.for_provider(provider, "TTS_LANGUAGE") or fallback
        if provider == "qwen3tts":
            return {"ja": "Japanese", "en": "English", "zh": "Chinese", "auto": "Auto"}.get(value.lower(), value)
        return value

    def require_enabled(self) -> None:
        value = self.values.get("TTS_ENABLED", "true").strip().lower()
        if value in ("false", "0", "no", "off"):
            raise ValueError("TTS_ENABLED=false: speech generation is disabled (health and audit remain available)")
        if value not in ("true", "1", "yes", "on"):
            raise ValueError("TTS_ENABLED must be true or false")


def get_tts_settings() -> TTSSettings:
    # Do not mutate os.environ or expose other .env secrets in logs/config files.
    values = {k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None}
    values.update(os.environ)
    return TTSSettings(values)
