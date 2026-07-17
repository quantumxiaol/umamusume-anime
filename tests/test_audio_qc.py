from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import audio_qc


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="audio QC integration tests require ffmpeg and ffprobe",
)


def write_wav(
    path: Path,
    *,
    duration: float = 1.2,
    sample_rate: int = 24_000,
    amplitude: float = 0.2,
    frequency: float = 440.0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_count = round(duration * sample_rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        frames = bytearray()
        for index in range(sample_count):
            value = amplitude * math.sin(2.0 * math.pi * frequency * index / sample_rate)
            frames.extend(struct.pack("<h", round(value * 32767)))
        handle.writeframes(frames)


def write_script(path: Path, audio: Path, *, speaker: str = "test_speaker") -> None:
    path.write_text(
        json.dumps(
            {
                "projectId": "test",
                "lines": [
                    {
                        "id": "l001",
                        "speakerId": speaker,
                        "audio": audio.relative_to(path.parent).as_posix(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_check_writes_deterministic_basic_qc_reports(tmp_path: Path) -> None:
    audio = tmp_path / "audio" / "l001.wav"
    script = tmp_path / "demo_script.json"
    write_wav(audio)
    write_script(script, audio)

    arguments = ["check", "--repo-root", str(tmp_path), "--script", str(script)]
    assert audio_qc.main(arguments) == 0
    report_path = tmp_path / "demo_audio_qc" / "qc_report.json"
    retry_path = tmp_path / "demo_audio_qc" / "retry_plan.json"
    first_report = report_path.read_bytes()
    first_retry = retry_path.read_bytes()

    assert audio_qc.main(arguments) == 0
    assert report_path.read_bytes() == first_report
    assert retry_path.read_bytes() == first_retry

    report = json.loads(first_report)
    assert report["summary"] == {
        "total": 1,
        "pass": 1,
        "review": 0,
        "error": 0,
        "retryRecommended": 0,
        "roletoneErrors": 0,
    }
    line = report["lines"][0]
    assert line["metrics"]["sampleRate"] == 24_000
    assert line["metrics"]["channels"] == 1
    assert line["metrics"]["durationSec"] == pytest.approx(1.2, abs=0.001)
    assert -18.0 < line["metrics"]["rmsDbfs"] < -16.0
    assert line["metrics"]["clippingRatio"] == 0
    assert json.loads(first_retry)["items"] == []


def test_silence_is_reported_and_added_to_retry_plan(tmp_path: Path) -> None:
    audio = tmp_path / "audio" / "l001.wav"
    script = tmp_path / "silent_script.json"
    write_wav(audio, amplitude=0.0)
    write_script(script, audio)

    assert (
        audio_qc.main(["check", "--repo-root", str(tmp_path), "--script", str(script)])
        == 1
    )
    report = json.loads((tmp_path / "silent_audio_qc" / "qc_report.json").read_text())
    retry = json.loads((tmp_path / "silent_audio_qc" / "retry_plan.json").read_text())
    codes = {item["code"] for item in report["lines"][0]["issues"]}
    assert {"low_rms", "excessive_silence", "long_leading_silence", "long_trailing_silence"} <= codes
    assert retry["items"][0]["id"] == "l001"
    assert "excessive_silence" in retry["items"][0]["reasons"]


def test_non_retryable_script_error_still_fails_the_qc_gate(tmp_path: Path) -> None:
    audio = tmp_path / "audio" / "l001.wav"
    script = tmp_path / "missing_speaker_script.json"
    write_wav(audio)
    write_script(script, audio, speaker="")

    assert audio_qc.main(["check", "--repo-root", str(tmp_path), "--script", str(script)]) == 1
    report = json.loads(
        (tmp_path / "missing_speaker_audio_qc" / "qc_report.json").read_text()
    )
    assert report["summary"]["error"] == 1
    assert report["summary"]["retryRecommended"] == 0


def test_clipping_is_measured_and_added_to_retry_plan(tmp_path: Path) -> None:
    audio = tmp_path / "audio" / "l001.wav"
    script = tmp_path / "clipped_script.json"
    write_wav(audio, amplitude=1.0)
    write_script(script, audio)

    assert (
        audio_qc.main(["check", "--repo-root", str(tmp_path), "--script", str(script)])
        == 1
    )
    report = json.loads((tmp_path / "clipped_audio_qc" / "qc_report.json").read_text())
    line = report["lines"][0]
    assert line["metrics"]["clippingRatio"] > 0.001
    assert "clipping" in {item["code"] for item in line["issues"]}


def test_compare_is_dry_run_by_default_and_apply_keeps_backup(tmp_path: Path) -> None:
    current = tmp_path / "audio" / "l001.wav"
    candidate = tmp_path / "retry" / "test_speaker" / "l001.wav"
    script = tmp_path / "demo_script.json"
    write_wav(current, amplitude=0.0)
    write_wav(candidate, amplitude=0.2)
    write_script(script, current)
    old_hash = file_hash(current)
    candidate_hash = file_hash(candidate)

    base_arguments = [
        "compare",
        "--repo-root",
        str(tmp_path),
        "--script",
        str(script),
        "--candidates-dir",
        str(tmp_path / "retry"),
    ]
    assert audio_qc.main(base_arguments) == 0
    dry_report = json.loads((tmp_path / "retry" / "comparison_report.json").read_text())
    assert dry_report["comparisons"][0]["decision"] == "replace"
    assert dry_report["comparisons"][0]["applied"] is False
    assert dry_report["comparisons"][0]["current"]["sha256"] == old_hash
    assert dry_report["comparisons"][0]["candidate"]["sha256"] == candidate_hash
    assert file_hash(current) == old_hash

    assert audio_qc.main([*base_arguments, "--apply"]) == 0
    applied_report = json.loads((tmp_path / "retry" / "comparison_report.json").read_text())
    assert applied_report["comparisons"][0]["applied"] is True
    assert file_hash(current) == candidate_hash
    backup = tmp_path / "retry" / "replaced_originals" / "audio" / "l001.wav"
    assert file_hash(backup) == old_hash


def test_repeated_apply_preserves_each_replaced_current_wav(tmp_path: Path) -> None:
    current = tmp_path / "audio" / "l001.wav"
    candidate = tmp_path / "retry" / "test_speaker" / "l001.wav"
    script = tmp_path / "demo_script.json"
    write_wav(current, amplitude=0.0)
    write_wav(candidate, amplitude=0.02)
    write_script(script, current)
    original_hash = file_hash(current)
    first_replacement_hash = file_hash(candidate)
    arguments = [
        "compare",
        "--repo-root",
        str(tmp_path),
        "--script",
        str(script),
        "--candidates-dir",
        str(tmp_path / "retry"),
    ]

    assert audio_qc.main(arguments) == 0
    assert audio_qc.main([*arguments, "--apply"]) == 0
    assert file_hash(current) == first_replacement_hash

    write_wav(candidate, amplitude=0.2)
    second_replacement_hash = file_hash(candidate)
    assert audio_qc.main(arguments) == 0
    assert audio_qc.main([*arguments, "--apply"]) == 0

    backup_root = tmp_path / "retry" / "replaced_originals" / "audio"
    original_backup = backup_root / "l001.wav"
    versioned_backup = backup_root / f"l001.{first_replacement_hash}.wav"
    report = json.loads((tmp_path / "retry" / "comparison_report.json").read_text())
    assert file_hash(original_backup) == original_hash
    assert file_hash(versioned_backup) == first_replacement_hash
    assert file_hash(current) == second_replacement_hash
    assert report["comparisons"][0]["backup"] == (
        f"retry/replaced_originals/audio/l001.{first_replacement_hash}.wav"
    )


@pytest.mark.parametrize("changed_side", ["current", "candidate"])
def test_apply_refuses_audio_changed_since_review(tmp_path: Path, changed_side: str) -> None:
    current = tmp_path / "audio" / "l001.wav"
    candidate = tmp_path / "retry" / "test_speaker" / "l001.wav"
    script = tmp_path / "demo_script.json"
    write_wav(current, amplitude=0.0)
    write_wav(candidate, amplitude=0.02)
    write_script(script, current)
    arguments = [
        "compare",
        "--repo-root",
        str(tmp_path),
        "--script",
        str(script),
        "--candidates-dir",
        str(tmp_path / "retry"),
    ]
    assert audio_qc.main(arguments) == 0

    changed_path = current if changed_side == "current" else candidate
    write_wav(
        changed_path,
        duration=1.3 if changed_side == "current" else 1.2,
        amplitude=0.0 if changed_side == "current" else 0.2,
    )
    changed_hash = file_hash(changed_path)
    assert audio_qc.main([*arguments, "--apply"]) == 1

    report = json.loads((tmp_path / "retry" / "comparison_report.json").read_text())
    comparison = report["comparisons"][0]
    assert comparison["decision"] == "keep"
    assert comparison["reason"] == f"{changed_side}_changed_since_review"
    assert comparison["applied"] is False
    assert file_hash(changed_path) == changed_hash


@pytest.mark.parametrize(
    "issue_code",
    [
        "excessive_silence",
        "clipping",
        "low_rms",
        "duration_too_short",
        "channel_mismatch",
        "sample_rate_mismatch",
    ],
)
def test_objectively_unsafe_candidate_is_never_auto_replaced(issue_code: str) -> None:
    current = {
        "status": "review",
        "qualityScore": 20.0,
        "voiceSimilarity": None,
        "issues": [],
    }
    candidate = {
        "status": "review",
        "qualityScore": 100.0,
        "voiceSimilarity": None,
        "issues": [audio_qc.issue(issue_code, "warning", "unsafe", True)],
    }

    assert audio_qc.replacement_decision(
        current=current,
        candidate=candidate,
        min_score_improvement=0.5,
        min_quality_improvement=1.0,
    ) == ("keep", "candidate_failed_objective_qc")


def test_candidate_objective_quality_must_not_regress_even_if_current_has_an_error() -> None:
    current = {
        "status": "error",
        "qualityScore": 95.0,
        "voiceSimilarity": None,
        "issues": [audio_qc.issue("missing_speaker", "error", "script error", False)],
    }
    candidate = {
        "status": "pass",
        "qualityScore": 94.0,
        "voiceSimilarity": None,
        "issues": [],
    }

    assert audio_qc.replacement_decision(
        current=current,
        candidate=candidate,
        min_score_improvement=0.5,
        min_quality_improvement=1.0,
    ) == ("keep", "objective_quality_regressed")


def test_duration_aware_roletone_thresholds() -> None:
    args = audio_qc.build_parser().parse_args(
        ["check", "--script", "draft/example.json", "--roletone"]
    )
    assert audio_qc.roletone_threshold(0.4, args) == 65.0
    assert audio_qc.roletone_threshold(0.8, args) == 70.0
    assert audio_qc.roletone_threshold(1.5, args) == 78.0
    assert audio_qc.roletone_threshold(2.0, args) == 82.0


def test_in_process_roletone_loads_one_model_for_multiple_speakers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Result:
        def __init__(self, reference: Path, candidate: Path) -> None:
            self.reference = reference
            self.candidate = candidate

        def to_dict(self) -> dict[str, object]:
            return {
                "reference": str(self.reference),
                "candidate": str(self.candidate),
                "cosine": 0.8,
                "score": 90.0,
                "verdict": "likely",
                "model": "sv",
            }

    class Scorer:
        def __init__(self) -> None:
            self.loads = 0
            self.comparisons = 0

        def load(self) -> None:
            self.loads += 1

        def compare_many(self, reference: Path, candidates: list[Path]) -> list[Result]:
            self.comparisons += 1
            return [Result(reference, candidate) for candidate in candidates]

    scorer = Scorer()
    monkeypatch.setattr(audio_qc, "make_roletone_scorer", lambda **_kwargs: scorer)
    for speaker in ("a", "b"):
        character = tmp_path / "characters" / speaker
        character.mkdir(parents=True)
        (character / "reference.wav").write_bytes(b"reference")
    paths = [tmp_path / "a.wav", tmp_path / "b.wav"]
    entries = []
    for index, (speaker, path) in enumerate(zip(("a", "b"), paths, strict=True), start=1):
        entries.append(
            {
                "id": f"l{index}",
                "index": index,
                "speakerId": speaker,
                "audio": path.name,
                "resolvedPath": str(path),
                "exists": True,
                "status": "pass",
                "issues": [],
                "metrics": {"durationSec": 2.0},
                "qualityScore": 100.0,
                "voiceSimilarity": None,
            }
        )
    context = audio_qc.Context(
        repo_root=tmp_path,
        script_path=tmp_path / "script.json",
        script={},
        lines=[],
    )
    args = audio_qc.build_parser().parse_args(
        [
            "check",
            "--repo-root",
            str(tmp_path),
            "--script",
            str(context.script_path),
            "--characters-root",
            "characters",
            "--roletone",
        ]
    )

    speakers = audio_qc.apply_roletone(entries=entries, context=context, args=args)

    assert scorer.loads == 1
    assert scorer.comparisons == 2
    assert [speaker["roletoneStatus"] for speaker in speakers] == ["complete", "complete"]
    assert all(entry["voiceSimilarity"]["passed"] for entry in entries)


def test_roletone_isolates_one_failed_candidate_without_losing_speaker_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Result:
        def __init__(self, candidate: Path) -> None:
            self.candidate = candidate

        def to_dict(self) -> dict[str, object]:
            return {
                "reference": str(tmp_path / "characters" / "a" / "reference.wav"),
                "candidate": str(self.candidate),
                "cosine": 0.8,
                "score": 90.0,
                "verdict": "likely",
                "model": "sv",
            }

    class Scorer:
        def load(self) -> None:
            return None

        def compare_many(self, _reference: Path, candidates: list[Path]) -> list[Result]:
            if any(candidate.name == "too-short.wav" for candidate in candidates):
                raise RuntimeError("candidate is shorter than the model window")
            return [Result(candidate) for candidate in candidates]

    monkeypatch.setattr(audio_qc, "make_roletone_scorer", lambda **_kwargs: Scorer())
    character = tmp_path / "characters" / "a"
    character.mkdir(parents=True)
    (character / "reference.wav").write_bytes(b"reference")
    entries = []
    for index, filename in enumerate(("good-a.wav", "too-short.wav", "good-b.wav"), start=1):
        path = tmp_path / filename
        entries.append(
            {
                "id": f"l{index}",
                "index": index,
                "speakerId": "a",
                "audio": filename,
                "resolvedPath": str(path),
                "exists": True,
                "status": "pass",
                "issues": [],
                "metrics": {"durationSec": 2.0},
                "qualityScore": 100.0,
                "voiceSimilarity": None,
            }
        )
    context = audio_qc.Context(
        repo_root=tmp_path,
        script_path=tmp_path / "script.json",
        script={},
        lines=[],
    )
    args = audio_qc.build_parser().parse_args(
        [
            "check",
            "--repo-root",
            str(tmp_path),
            "--script",
            str(context.script_path),
            "--characters-root",
            "characters",
            "--roletone",
        ]
    )

    speakers = audio_qc.apply_roletone(entries=entries, context=context, args=args)

    assert speakers[0]["roletoneStatus"] == "partial"
    assert entries[0]["voiceSimilarity"]["passed"] is True
    assert entries[2]["voiceSimilarity"]["passed"] is True
    assert entries[1]["voiceSimilarity"]["status"] == "error"
    assert {item["code"] for item in entries[1]["issues"]} == {"roletone_failed"}


def test_roletone_mismatch_and_short_leniency_are_review_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Result:
        def __init__(self, candidate: Path, *, score: float, verdict: str) -> None:
            self.candidate = candidate
            self.score = score
            self.verdict = verdict

        def to_dict(self) -> dict[str, object]:
            return {
                "reference": str(tmp_path / "characters" / "a" / "reference.wav"),
                "candidate": str(self.candidate),
                "cosine": 0.8,
                "score": self.score,
                "verdict": self.verdict,
                "model": "sv",
            }

    class Scorer:
        def load(self) -> None:
            return None

        def compare_many(self, _reference: Path, candidates: list[Path]) -> list[Result]:
            return [
                Result(
                    candidate,
                    score=90.0 if candidate.name == "mismatch.wav" else 75.0,
                    verdict="mismatch" if candidate.name == "mismatch.wav" else "borderline",
                )
                for candidate in candidates
            ]

    monkeypatch.setattr(audio_qc, "make_roletone_scorer", lambda **_kwargs: Scorer())
    character = tmp_path / "characters" / "a"
    character.mkdir(parents=True)
    (character / "reference.wav").write_bytes(b"reference")
    mismatch_path = tmp_path / "mismatch.wav"
    short_path = tmp_path / "short.wav"
    entries = [
        {
            "id": "mismatch",
            "index": 1,
            "speakerId": "a",
            "audio": mismatch_path.name,
            "resolvedPath": str(mismatch_path),
            "exists": True,
            "status": "pass",
            "issues": [],
            "metrics": {"durationSec": 2.0},
            "qualityScore": 100.0,
            "voiceSimilarity": None,
        },
        {
            "id": "short",
            "index": 2,
            "speakerId": "a",
            "audio": short_path.name,
            "resolvedPath": str(short_path),
            "exists": True,
            "status": "pass",
            "issues": [],
            "metrics": {"durationSec": 0.8},
            "qualityScore": 100.0,
            "voiceSimilarity": None,
        },
    ]
    context = audio_qc.Context(
        repo_root=tmp_path,
        script_path=tmp_path / "script.json",
        script={},
        lines=[],
    )
    args = audio_qc.build_parser().parse_args(
        [
            "check",
            "--repo-root",
            str(tmp_path),
            "--script",
            str(context.script_path),
            "--characters-root",
            "characters",
            "--roletone",
        ]
    )

    audio_qc.apply_roletone(entries=entries, context=context, args=args)

    mismatch = entries[0]
    short = entries[1]
    assert mismatch["voiceSimilarity"]["passed"] is False
    assert mismatch["voiceSimilarity"]["status"] == "review"
    assert "roletone_verdict_mismatch" in {item["code"] for item in mismatch["issues"]}
    assert short["voiceSimilarity"]["passed"] is False
    assert short["voiceSimilarity"]["lenientPass"] is True
    assert short["status"] == "review"
    short_issue = next(
        item for item in short["issues"] if item["code"] == "short_line_roletone_review"
    )
    assert short_issue["retryRecommended"] is False


def test_explicit_roletone_comparison_does_not_fallback_when_scores_are_missing() -> None:
    current = {"status": "pass", "qualityScore": 10.0, "voiceSimilarity": None}
    candidate = {"status": "pass", "qualityScore": 100.0, "voiceSimilarity": None}

    assert audio_qc.replacement_decision(
        current=current,
        candidate=candidate,
        min_score_improvement=0.5,
        min_quality_improvement=1.0,
        require_voice_score=True,
    ) == ("keep", "roletone_score_unavailable")


def test_roletone_mismatch_is_never_auto_applied_even_if_marked_passed() -> None:
    current = {
        "status": "pass",
        "qualityScore": 95.0,
        "voiceSimilarity": {"score": 84.0, "passed": True, "verdict": "likely_match"},
    }
    candidate = {
        "status": "pass",
        "qualityScore": 100.0,
        "voiceSimilarity": {"score": 99.0, "passed": True, "verdict": "mismatch"},
    }

    assert audio_qc.replacement_decision(
        current=current,
        candidate=candidate,
        min_score_improvement=0.5,
        min_quality_improvement=1.0,
        require_voice_score=True,
    ) == ("keep", "candidate_roletone_verdict_mismatch")


def test_roletone_retry_replaces_only_when_score_improves_without_quality_regression() -> None:
    current = {
        "status": "pass",
        "qualityScore": 95.0,
        "voiceSimilarity": {"score": 84.0, "passed": True},
    }
    better = {
        "status": "pass",
        "qualityScore": 95.0,
        "voiceSimilarity": {"score": 88.0, "passed": True},
    }
    degraded = {
        "status": "pass",
        "qualityScore": 94.0,
        "voiceSimilarity": {"score": 90.0, "passed": True},
    }

    assert audio_qc.replacement_decision(
        current=current,
        candidate=better,
        min_score_improvement=0.5,
        min_quality_improvement=1.0,
        require_voice_score=True,
    ) == ("replace", "roletone_score_improved_without_quality_regression")
    assert audio_qc.replacement_decision(
        current=current,
        candidate=degraded,
        min_score_improvement=0.5,
        min_quality_improvement=1.0,
        require_voice_score=True,
    ) == ("keep", "objective_quality_regressed")
