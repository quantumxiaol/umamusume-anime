"""Small shared helpers; production JSON remains unchanged."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def lines(script: dict) -> list[dict]:
    result = script.get("lines")
    if result is None:
        result = [line for scene in script.get("scenes", []) for line in scene.get("lines", [])]
    if not isinstance(result, list) or not result:
        raise ValueError("Script has no lines")
    ids = [line.get("id") for line in result]
    if any(not isinstance(x, str) or not x.strip() for x in ids) or len(ids) != len(set(ids)):
        raise ValueError("Every line needs a unique, stable id; do not renumber existing lines")
    paths = [str(Path(line["audio"]).expanduser().resolve()) for line in result if line.get("audio")]
    if len(paths) != len(set(paths)):
        raise ValueError("Multiple lines share an audio output path")
    return result
