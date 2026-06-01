from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _runtime_root() -> Path:
    root = os.getenv("ROLETONE_HOME") or Path.cwd()
    return Path(root).expanduser().resolve()


def _load_env() -> None:
    env_file = os.getenv("ROLETONE_ENV_FILE")
    if env_file:
        load_dotenv(Path(env_file).expanduser().resolve())
        return
    load_dotenv(_runtime_root() / ".env")


_load_env()


MODEL_ALIASES = {
    "sv": "microsoft/wavlm-base-plus-sv",
    "speaker": "microsoft/wavlm-base-plus-sv",
    "speaker-verification": "microsoft/wavlm-base-plus-sv",
    "wavlm-base-plus-sv": "microsoft/wavlm-base-plus-sv",
    "base-sv": "microsoft/wavlm-base-plus-sv",
    "base": "microsoft/wavlm-base-plus",
    "base-plus": "microsoft/wavlm-base-plus",
    "wavlm-base-plus": "microsoft/wavlm-base-plus",
    "large": "microsoft/wavlm-large",
    "wavlm-large": "microsoft/wavlm-large",
}


@dataclass(frozen=True)
class SimilarityThresholds:
    match: float = 0.86
    likely: float = 0.75
    borderline: float = 0.65


@dataclass(frozen=True)
class RoleToneSettings:
    model: str
    model_id: str
    embedding_mode: str
    device: str
    sample_rate: int
    trim_top_db: float | None
    hf_home: Path
    cache_dir: Path | None
    local_files_only: bool
    thresholds: SimilarityThresholds


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _runtime_root() / path
    return path.resolve()


def configure_hf_home(value: str | Path | None = None) -> Path:
    raw = value or os.getenv("HF_HOME") or "./modelsweights/huggingface"
    hf_home = resolve_project_path(raw)
    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(hf_home)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    return hf_home


def resolve_model_name(model: str | None) -> str:
    raw = (model or os.getenv("ROLETONE_MODEL") or "wavlm-base-plus-sv").strip()
    return MODEL_ALIASES.get(raw.lower(), raw)


def default_embedding_mode(model_id: str, requested: str | None = None) -> str:
    mode = (requested or os.getenv("ROLETONE_EMBEDDING_MODE") or "auto").strip().lower()
    if mode not in {"auto", "xvector", "pooled"}:
        raise ValueError("embedding mode must be one of: auto, xvector, pooled")
    if mode != "auto":
        return mode
    return "xvector" if model_id.lower().endswith("-sv") else "pooled"


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_optional_float(name: str, default: float | None) -> float | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    if value.strip().lower() in {"none", "false", "off", "disable", "disabled"}:
        return None
    parsed = float(value)
    return parsed if parsed > 0 else None


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_settings(
    *,
    model: str | None = None,
    embedding_mode: str | None = None,
    device: str | None = None,
    hf_home: str | Path | None = None,
    cache_dir: str | Path | None = None,
    local_files_only: bool | None = None,
) -> RoleToneSettings:
    resolved_hf_home = configure_hf_home(hf_home)
    cache_dir_raw = cache_dir or os.getenv("ROLETONE_CACHE_DIR")
    resolved_cache_dir = resolve_project_path(cache_dir_raw) if cache_dir_raw else None
    if resolved_cache_dir is not None:
        resolved_cache_dir.mkdir(parents=True, exist_ok=True)

    resolved_local_files_only = (
        local_files_only
        if local_files_only is not None
        else _env_bool("ROLETONE_LOCAL_FILES_ONLY") or _env_bool("HF_HUB_OFFLINE")
    )
    if resolved_local_files_only:
        os.environ["HF_HUB_OFFLINE"] = "1"

    model_id = resolve_model_name(model)
    return RoleToneSettings(
        model=model or os.getenv("ROLETONE_MODEL") or "wavlm-base-plus-sv",
        model_id=model_id,
        embedding_mode=default_embedding_mode(model_id, embedding_mode),
        device=device or os.getenv("ROLETONE_DEVICE") or "auto",
        sample_rate=_env_int("ROLETONE_SAMPLE_RATE", 16000),
        trim_top_db=_env_optional_float("ROLETONE_TRIM_TOP_DB", 30.0),
        hf_home=resolved_hf_home,
        cache_dir=resolved_cache_dir,
        local_files_only=resolved_local_files_only,
        thresholds=SimilarityThresholds(
            match=_env_float("ROLETONE_MATCH_THRESHOLD", 0.86),
            likely=_env_float("ROLETONE_LIKELY_THRESHOLD", 0.75),
            borderline=_env_float("ROLETONE_BORDERLINE_THRESHOLD", 0.65),
        ),
    )


configure_hf_home()
