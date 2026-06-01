from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np
import torch
import torchaudio


@dataclass(frozen=True)
class AudioData:
    waveform: torch.Tensor
    sample_rate: int
    duration_sec: float


def load_audio(
    path: str | Path,
    *,
    target_sample_rate: int = 16_000,
    trim_top_db: float | None = 30.0,
) -> AudioData:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"audio file not found: {source}")

    waveform, sample_rate = _load_with_torchaudio(source)
    waveform = _to_mono_float32(waveform)
    waveform = torch.nan_to_num(waveform)

    if sample_rate != target_sample_rate:
        waveform = torchaudio.functional.resample(waveform, sample_rate, target_sample_rate)
        sample_rate = target_sample_rate

    if trim_top_db is not None:
        waveform = _trim_silence(waveform, trim_top_db)

    if waveform.numel() == 0:
        raise ValueError(f"audio file is empty after preprocessing: {source}")

    duration_sec = waveform.numel() / float(sample_rate)
    return AudioData(waveform=waveform.contiguous(), sample_rate=sample_rate, duration_sec=duration_sec)


def _load_with_torchaudio(path: Path) -> tuple[torch.Tensor, int]:
    try:
        return torchaudio.load(str(path))
    except Exception:
        audio, sample_rate = librosa.load(str(path), sr=None, mono=False)
        if audio.ndim == 1:
            audio = audio[np.newaxis, :]
        return torch.from_numpy(np.asarray(audio, dtype=np.float32)), int(sample_rate)


def _to_mono_float32(waveform: torch.Tensor) -> torch.Tensor:
    waveform = waveform.to(torch.float32)
    if waveform.ndim == 1:
        return waveform
    if waveform.ndim != 2:
        raise ValueError(f"expected 1D or 2D audio tensor, got shape={tuple(waveform.shape)}")
    if waveform.shape[0] == 1:
        return waveform.squeeze(0)
    return waveform.mean(dim=0)


def _trim_silence(waveform: torch.Tensor, top_db: float) -> torch.Tensor:
    audio = waveform.detach().cpu().numpy()
    trimmed, _ = librosa.effects.trim(audio, top_db=top_db)
    if trimmed.size == 0:
        return waveform
    return torch.from_numpy(np.asarray(trimmed, dtype=np.float32))
