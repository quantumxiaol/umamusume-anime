from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import torch
import torch.nn.functional as F

from .audio import load_audio
from .config import SimilarityThresholds, get_settings


@dataclass(frozen=True)
class AudioEmbedding:
    source: str
    embedding: torch.Tensor
    duration_sec: float


@dataclass(frozen=True)
class ScoreResult:
    reference: str
    candidate: str
    cosine: float
    similarity: float
    score: float
    verdict: str
    reference_duration_sec: float
    candidate_duration_sec: float
    model: str
    model_id: str
    embedding_mode: str
    device: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class WavLMScorer:
    def __init__(
        self,
        *,
        model: str | None = None,
        embedding_mode: str | None = None,
        device: str | None = None,
        hf_home: str | Path | None = None,
        cache_dir: str | Path | None = None,
        local_files_only: bool | None = None,
    ) -> None:
        self.settings = get_settings(
            model=model,
            embedding_mode=embedding_mode,
            device=device,
            hf_home=hf_home,
            cache_dir=cache_dir,
            local_files_only=local_files_only,
        )
        self.device = self._resolve_device(self.settings.device)
        self._feature_extractor = None
        self._model = None
        self._load_lock = threading.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def model_info(self) -> dict[str, object]:
        return {
            "model": self.settings.model,
            "model_id": self.settings.model_id,
            "embedding_mode": self.settings.embedding_mode,
            "device": str(self.device),
            "sample_rate": self.settings.sample_rate,
            "hf_home": str(self.settings.hf_home),
            "cache_dir": str(self.settings.cache_dir) if self.settings.cache_dir else None,
            "local_files_only": self.settings.local_files_only,
            "loaded": self.is_loaded,
        }

    def load(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return

            from transformers import AutoFeatureExtractor, AutoModel, AutoModelForAudioXVector

            kwargs = {}
            if self.settings.cache_dir is not None:
                kwargs["cache_dir"] = str(self.settings.cache_dir)
            if self.settings.local_files_only:
                kwargs["local_files_only"] = True

            self._feature_extractor = AutoFeatureExtractor.from_pretrained(
                self.settings.model_id,
                **kwargs,
            )
            if self.settings.embedding_mode == "xvector":
                model = AutoModelForAudioXVector.from_pretrained(self.settings.model_id, **kwargs)
            else:
                model = AutoModel.from_pretrained(self.settings.model_id, **kwargs)

            model.eval()
            self._model = model.to(self.device)

    def embed_file(self, path: str | Path) -> AudioEmbedding:
        self.load()
        assert self._feature_extractor is not None
        assert self._model is not None

        audio = load_audio(
            path,
            target_sample_rate=self.settings.sample_rate,
            trim_top_db=self.settings.trim_top_db,
        )
        inputs = self._feature_extractor(
            audio.waveform.detach().cpu().numpy(),
            sampling_rate=audio.sample_rate,
            return_tensors="pt",
            padding=False,
        )
        inputs = {key: value.to(self.device) for key, value in inputs.items()}

        with torch.inference_mode():
            outputs = self._model(**inputs)
            if self.settings.embedding_mode == "xvector":
                embedding = outputs.embeddings
            else:
                embedding = outputs.last_hidden_state.mean(dim=1)

        embedding = F.normalize(embedding.squeeze(0).float().cpu(), dim=0)
        return AudioEmbedding(source=str(path), embedding=embedding, duration_sec=audio.duration_sec)

    def compare_files(self, reference: str | Path, candidate: str | Path) -> ScoreResult:
        reference_embedding = self.embed_file(reference)
        candidate_embedding = self.embed_file(candidate)
        return self.compare_embeddings(reference_embedding, candidate_embedding)

    def compare_many(self, reference: str | Path, candidates: Iterable[str | Path]) -> list[ScoreResult]:
        reference_embedding = self.embed_file(reference)
        return [
            self.compare_embeddings(reference_embedding, self.embed_file(candidate))
            for candidate in candidates
        ]

    def compare_embeddings(self, reference: AudioEmbedding, candidate: AudioEmbedding) -> ScoreResult:
        cosine = float(torch.dot(reference.embedding, candidate.embedding).item())
        similarity = _cosine_to_unit_score(cosine)
        return ScoreResult(
            reference=reference.source,
            candidate=candidate.source,
            cosine=round(cosine, 6),
            similarity=round(similarity, 6),
            score=round(similarity * 100.0, 3),
            verdict=verdict_for_cosine(cosine, self.settings.thresholds),
            reference_duration_sec=round(reference.duration_sec, 3),
            candidate_duration_sec=round(candidate.duration_sec, 3),
            model=self.settings.model,
            model_id=self.settings.model_id,
            embedding_mode=self.settings.embedding_mode,
            device=str(self.device),
        )

    @staticmethod
    def _resolve_device(requested: str) -> torch.device:
        value = requested.strip().lower()
        if value != "auto":
            if value.startswith("cuda") and not torch.cuda.is_available():
                raise RuntimeError("CUDA was requested, but torch.cuda.is_available() is false")
            if value == "mps" and (
                getattr(torch.backends, "mps", None) is None or not torch.backends.mps.is_available()
            ):
                raise RuntimeError("MPS was requested, but torch.backends.mps.is_available() is false")
            return torch.device(value)
        if torch.cuda.is_available():
            return torch.device("cuda")
        if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")


def available_devices() -> list[dict[str, object]]:
    devices: list[dict[str, object]] = [{"name": "cpu", "available": True}]
    devices.append(
        {
            "name": "mps",
            "available": getattr(torch.backends, "mps", None) is not None
            and torch.backends.mps.is_available(),
        }
    )
    cuda_available = torch.cuda.is_available()
    devices.append(
        {
            "name": "cuda",
            "available": cuda_available,
            "count": torch.cuda.device_count() if cuda_available else 0,
        }
    )
    if cuda_available:
        for index in range(torch.cuda.device_count()):
            devices.append(
                {
                    "name": f"cuda:{index}",
                    "available": True,
                    "device_name": torch.cuda.get_device_name(index),
                }
            )
    return devices


def _cosine_to_unit_score(cosine: float) -> float:
    return max(0.0, min(1.0, (cosine + 1.0) / 2.0))


def verdict_for_cosine(cosine: float, thresholds: SimilarityThresholds) -> str:
    if cosine >= thresholds.match:
        return "match"
    if cosine >= thresholds.likely:
        return "likely_match"
    if cosine >= thresholds.borderline:
        return "borderline"
    return "mismatch"
