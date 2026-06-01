from __future__ import annotations

import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .config import get_settings
from .scorer import WavLMScorer


app = FastAPI(
    title="RoleTone",
    description="WavLM-based similarity scoring for cloned TTS audio.",
    version="0.1.0",
)


class PathScoreRequest(BaseModel):
    reference: str = Field(..., description="Path to reference audio.")
    candidates: list[str] = Field(..., min_length=1, description="Paths to generated candidate audio.")
    model: str | None = None
    embedding_mode: str | None = None
    device: str | None = None
    hf_home: str | None = None
    cache_dir: str | None = None
    local_files_only: bool | None = None


@app.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "ok": True,
        "model": settings.model,
        "model_id": settings.model_id,
        "embedding_mode": settings.embedding_mode,
        "device": settings.device,
        "hf_home": str(settings.hf_home),
        "cache_dir": str(settings.cache_dir) if settings.cache_dir else None,
        "local_files_only": settings.local_files_only,
    }


@app.get("/ready")
def ready() -> dict[str, object]:
    scorer = get_scorer(None, None, None, None, None, None)
    try:
        scorer.load()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, **scorer.model_info()}


@app.post("/score")
async def score_upload(
    reference: UploadFile = File(...),
    candidate: UploadFile = File(...),
    model: str | None = Form(default=None),
    embedding_mode: str | None = Form(default=None),
    device: str | None = Form(default=None),
    hf_home: str | None = Form(default=None),
    cache_dir: str | None = Form(default=None),
    local_files_only: bool | None = Form(default=None),
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="roletone-") as tmp:
        tmpdir = Path(tmp)
        reference_path = await save_upload(reference, tmpdir, "reference")
        candidate_path = await save_upload(candidate, tmpdir, "candidate")
        scorer = get_scorer(model, embedding_mode, device, hf_home, cache_dir, local_files_only)
        try:
            result = scorer.compare_files(reference_path, candidate_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = result.to_dict()
    payload["reference"] = reference.filename or "reference"
    payload["candidate"] = candidate.filename or "candidate"
    return payload


@app.post("/score/batch")
async def score_batch_upload(
    reference: UploadFile = File(...),
    candidates: list[UploadFile] = File(...),
    model: str | None = Form(default=None),
    embedding_mode: str | None = Form(default=None),
    device: str | None = Form(default=None),
    hf_home: str | None = Form(default=None),
    cache_dir: str | None = Form(default=None),
    local_files_only: bool | None = Form(default=None),
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="roletone-") as tmp:
        tmpdir = Path(tmp)
        reference_path = await save_upload(reference, tmpdir, "reference")
        candidate_paths = [
            await save_upload(candidate, tmpdir, f"candidate-{index}")
            for index, candidate in enumerate(candidates)
        ]
        scorer = get_scorer(model, embedding_mode, device, hf_home, cache_dir, local_files_only)
        try:
            results = scorer.compare_many(reference_path, candidate_paths)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = []
    for upload, result in zip(candidates, results, strict=True):
        item = result.to_dict()
        item["reference"] = reference.filename or "reference"
        item["candidate"] = upload.filename or "candidate"
        payload.append(item)
    return {"results": payload}


@app.post("/score/paths")
def score_paths(request: PathScoreRequest) -> dict[str, object]:
    scorer = get_scorer(
        request.model,
        request.embedding_mode,
        request.device,
        request.hf_home,
        request.cache_dir,
        request.local_files_only,
    )
    try:
        results = scorer.compare_many(request.reference, request.candidates)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"results": [result.to_dict() for result in results]}


@lru_cache(maxsize=8)
def get_scorer(
    model: str | None,
    embedding_mode: str | None,
    device: str | None,
    hf_home: str | None,
    cache_dir: str | None,
    local_files_only: bool | None,
) -> WavLMScorer:
    return WavLMScorer(
        model=model,
        embedding_mode=embedding_mode,
        device=device,
        hf_home=hf_home,
        cache_dir=cache_dir,
        local_files_only=local_files_only,
    )


async def save_upload(upload: UploadFile, directory: Path, stem: str) -> Path:
    suffix = Path(upload.filename or "").suffix or ".wav"
    path = directory / f"{stem}{suffix}"
    with path.open("wb") as handle:
        shutil.copyfileobj(upload.file, handle)
    await upload.seek(0)
    return path
