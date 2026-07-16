from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 2
DEFAULT_CHARACTERS_ROOT = Path("characters")
DEFAULT_FFMPEG = "ffmpeg"
DEFAULT_FFPROBE = "ffprobe"
DEFAULT_ROLETONE = Path(".venv/bin/roletone")
DEFAULT_HF_HOME = Path("modelsweights/huggingface")
DEFAULT_NUMBA_CACHE = Path("/private/tmp/roletone_numba_cache")
DECODE_SAMPLE_RATE = 16_000
OBJECTIVE_BLOCKING_CODES = frozenset(
    {
        "missing_audio",
        "hash_failed",
        "changed_during_analysis",
        "probe_failed",
        "decode_failed",
        "duration_too_short",
        "duration_too_long",
        "sample_rate_mismatch",
        "channel_mismatch",
        "low_rms",
        "excessive_silence",
        "clipping",
        "long_leading_silence",
        "long_trailing_silence",
    }
)


class AudioQcError(RuntimeError):
    pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return check_command(args)
        if args.command == "compare":
            return compare_command(args)
        parser.error(f"unsupported command: {args.command}")
    except AudioQcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio_qc",
        description="Validate script WAV files and optionally score speaker similarity with RoleTone.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check",
        help="Write qc_report.json and retry_plan.json for the audio in a script.",
    )
    add_common_args(check)
    check.add_argument(
        "--output-dir",
        help="Output directory. Defaults to draft/<script-name>_audio_qc/.",
    )
    check.add_argument("--report", help="Override the qc_report.json path.")
    check.add_argument("--retry-plan", help="Override the retry_plan.json path.")

    compare = subparsers.add_parser(
        "compare",
        help="Compare retry WAVs with current WAVs and optionally apply only improvements.",
    )
    add_common_args(compare)
    compare.add_argument(
        "--candidates-dir",
        required=True,
        help="Retry WAV root. Files may be at <root>/<speaker>/<line>.wav or anywhere below root.",
    )
    compare.add_argument(
        "--output",
        help="Comparison report path. Defaults to <candidates-dir>/comparison_report.json.",
    )
    compare.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply replacements approved by the existing comparison report. Run the same "
            "command once without --apply to create the reviewed, hash-bound report first."
        ),
    )
    compare.add_argument(
        "--backup-dir",
        help="Backup root used with --apply. Defaults beside the comparison report.",
    )
    compare.add_argument(
        "--min-score-improvement",
        type=float,
        default=0.5,
        help="Minimum RoleTone score gain required for replacement (default: 0.5).",
    )
    compare.add_argument(
        "--min-quality-improvement",
        type=float,
        default=1.0,
        help="Minimum objective quality score gain when RoleTone is unavailable (default: 1.0).",
    )
    return parser


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--script", required=True, help="Structured script JSON.")
    parser.add_argument("--repo-root", default=".", help="Root used to resolve script audio paths.")
    parser.add_argument(
        "--characters-root",
        default=str(DEFAULT_CHARACTERS_ROOT),
        help="Character assets root, relative to --repo-root by default.",
    )
    parser.add_argument("--ffmpeg", default=DEFAULT_FFMPEG)
    parser.add_argument("--ffprobe", default=DEFAULT_FFPROBE)
    parser.add_argument(
        "--expected-sample-rate",
        type=positive_int,
        help="Warn when a WAV uses another sample rate. Omit to accept 24 kHz and 44.1 kHz alike.",
    )
    parser.add_argument("--expected-channels", type=positive_int, default=1)
    parser.add_argument("--min-duration", type=nonnegative_float, default=0.12)
    parser.add_argument("--max-duration", type=positive_float, default=60.0)
    parser.add_argument("--silence-dbfs", type=float, default=-50.0)
    parser.add_argument("--max-silent-ratio", type=unit_float, default=0.85)
    parser.add_argument("--min-rms-dbfs", type=float, default=-45.0)
    parser.add_argument("--max-clipping-ratio", type=unit_float, default=0.001)
    parser.add_argument("--max-edge-silence", type=nonnegative_float, default=0.75)
    parser.add_argument(
        "--roletone",
        action="store_true",
        help="Explicitly enable local, offline RoleTone scoring grouped by speaker.",
    )
    parser.add_argument("--roletone-cli", default=str(DEFAULT_ROLETONE))
    parser.add_argument(
        "--roletone-runner",
        choices=("in-process", "cli"),
        default="in-process",
        help=(
            "Use one in-process WavLM model for every speaker (default), or invoke the "
            "repo-local CLI once per speaker."
        ),
    )
    parser.add_argument("--roletone-model", default="sv")
    parser.add_argument("--roletone-device", default="cpu")
    parser.add_argument("--roletone-hf-home", default=str(DEFAULT_HF_HOME))
    parser.add_argument("--roletone-numba-cache", default=str(DEFAULT_NUMBA_CACHE))
    parser.add_argument(
        "--roletone-online",
        action="store_true",
        help="Allow RoleTone model lookup online. Offline mode is the default.",
    )
    parser.add_argument("--roletone-threshold-very-short", type=float, default=65.0)
    parser.add_argument("--roletone-threshold-short", type=float, default=70.0)
    parser.add_argument("--roletone-threshold-medium", type=float, default=78.0)
    parser.add_argument("--roletone-threshold-normal", type=float, default=82.0)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def unit_float(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def check_command(args: argparse.Namespace) -> int:
    context = load_context(args)
    entries = analyze_script_audio(context=context, args=args)
    speakers = apply_roletone(entries=entries, context=context, args=args) if args.roletone else speaker_index(entries)
    report = build_qc_report(context=context, entries=entries, speakers=speakers, args=args)
    retry_plan = build_retry_plan(context=context, report=report)

    output_dir = default_qc_output_dir(context.script_path)
    if args.output_dir:
        output_dir = resolve_cli_path(args.output_dir, context.repo_root)
    report_path = resolve_cli_path(args.report, context.repo_root) if args.report else output_dir / "qc_report.json"
    retry_path = (
        resolve_cli_path(args.retry_plan, context.repo_root)
        if args.retry_plan
        else output_dir / "retry_plan.json"
    )
    write_json_atomic(report_path, report)
    write_json_atomic(retry_path, retry_plan)
    print(f"qc_report={display_path(report_path, context.repo_root)}")
    print(f"retry_plan={display_path(retry_path, context.repo_root)}")
    print(
        "audio_qc "
        f"pass={report['summary']['pass']} "
        f"review={report['summary']['review']} "
        f"error={report['summary']['error']} "
        f"retry={report['summary']['retryRecommended']}"
    )
    roletone_failed = any(speaker["roletoneStatus"] == "error" for speaker in speakers)
    qc_failed = bool(report["summary"]["error"])
    return 1 if retry_plan["items"] or roletone_failed or qc_failed else 0


def compare_command(args: argparse.Namespace) -> int:
    context = load_context(args)
    candidates_root = resolve_cli_path(args.candidates_dir, context.repo_root)
    if not candidates_root.is_dir():
        raise AudioQcError(f"candidates directory not found: {candidates_root}")
    output_path = (
        resolve_cli_path(args.output, context.repo_root)
        if args.output
        else candidates_root / "comparison_report.json"
    )
    backup_root = (
        resolve_cli_path(args.backup_dir, context.repo_root)
        if args.backup_dir
        else output_path.parent / "replaced_originals"
    )
    settings = comparison_settings(args)
    script_sha256 = sha256_file(context.script_path)
    reviewed_report = None
    if args.apply:
        reviewed_report = load_reviewed_report(
            output_path=output_path,
            context=context,
            candidates_root=candidates_root,
            script_sha256=script_sha256,
            settings=settings,
        )

    current_entries = analyze_script_audio(context=context, args=args)
    candidate_entries: list[dict[str, Any]] = []
    for current in current_entries:
        candidate_path, lookup_issue = find_candidate(
            candidates_root=candidates_root,
            speaker_id=current["speakerId"],
            filename=Path(current["resolvedPath"]).name,
        )
        if candidate_path is None:
            candidate_entries.append(
                {
                    "id": current["id"],
                    "index": current["index"],
                    "speakerId": current["speakerId"],
                    "audio": None,
                    "resolvedPath": None,
                    "exists": False,
                    "status": "error" if lookup_issue == "ambiguous_candidate" else "missing",
                    "issues": (
                        [issue("ambiguous_candidate", "error", "multiple retry WAVs matched this line", False)]
                        if lookup_issue == "ambiguous_candidate"
                        else []
                    ),
                    "metrics": None,
                    "qualityScore": None,
                    "voiceSimilarity": None,
                    "sha256": None,
                }
            )
            continue
        candidate_entries.append(
            analyze_entry(
                line_id=current["id"],
                index=current["index"],
                speaker_id=current["speakerId"],
                audio_label=display_path(candidate_path, context.repo_root),
                audio_path=candidate_path,
                args=args,
            )
        )

    speaker_results: list[dict[str, Any]] = speaker_index(current_entries)
    if args.roletone:
        combined = [entry for entry in current_entries + candidate_entries if entry.get("resolvedPath")]
        speaker_results = apply_roletone(entries=combined, context=context, args=args)

    comparisons: list[dict[str, Any]] = []
    for current, candidate in zip(current_entries, candidate_entries, strict=True):
        decision, reason = replacement_decision(
            current=current,
            candidate=candidate,
            min_score_improvement=args.min_score_improvement,
            min_quality_improvement=args.min_quality_improvement,
            require_voice_score=bool(args.roletone),
        )
        comparisons.append(
            {
                "id": current["id"],
                "speakerId": current["speakerId"],
                "current": public_entry(current),
                "candidate": public_entry(candidate),
                "decision": decision,
                "reason": reason,
                "applied": False,
                "backup": None,
                "reviewedDecision": None,
            }
        )

    if args.apply:
        assert reviewed_report is not None
        bind_comparisons_to_reviewed(comparisons, reviewed_report)
        apply_replacements(
            comparisons=comparisons,
            current_entries=current_entries,
            candidate_entries=candidate_entries,
            backup_root=backup_root,
            repo_root=context.repo_root,
        )

    report = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "audio_qc_comparison",
        "script": display_path(context.script_path, context.repo_root),
        "scriptSha256": script_sha256,
        "candidatesDir": display_path(candidates_root, context.repo_root),
        "settings": settings,
        "summary": {
            "total": len(comparisons),
            "candidatesFound": sum(1 for item in comparisons if item["candidate"]["audio"]),
            "recommended": sum(1 for item in comparisons if item["decision"] == "replace"),
            "applied": sum(1 for item in comparisons if item["applied"]),
            "refused": sum(
                1
                for item in comparisons
                if item["reviewedDecision"] == "replace" and not item["applied"]
            ),
            "roletoneErrors": sum(
                1 for speaker in speaker_results if speaker["roletoneStatus"] == "error"
            ),
        },
        "speakers": speaker_results,
        "comparisons": comparisons,
    }
    write_json_atomic(output_path, report)
    print(f"comparison_report={display_path(output_path, context.repo_root)}")
    print(
        "audio_qc_compare "
        f"found={report['summary']['candidatesFound']} "
        f"recommended={report['summary']['recommended']} "
        f"applied={report['summary']['applied']} "
        f"refused={report['summary']['refused']}"
    )
    return 1 if args.apply and report["summary"]["refused"] else 0


def comparison_settings(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "expectedSampleRate": args.expected_sample_rate,
        "expectedChannels": args.expected_channels,
        "minDurationSec": args.min_duration,
        "maxDurationSec": args.max_duration,
        "silenceDbfs": args.silence_dbfs,
        "maxSilentRatio": args.max_silent_ratio,
        "minRmsDbfs": args.min_rms_dbfs,
        "maxClippingRatio": args.max_clipping_ratio,
        "maxEdgeSilenceSec": args.max_edge_silence,
        "roletoneEnabled": bool(args.roletone),
        "roletoneRunner": args.roletone_runner if args.roletone else None,
        "roletoneModel": args.roletone_model if args.roletone else None,
        "roletoneDevice": args.roletone_device if args.roletone else None,
        "roletoneOffline": bool(args.roletone and not args.roletone_online),
        "roletoneThresholds": {
            "under0.5s": args.roletone_threshold_very_short,
            "under1s": args.roletone_threshold_short,
            "under2s": args.roletone_threshold_medium,
            "normal": args.roletone_threshold_normal,
        },
        "minScoreImprovement": args.min_score_improvement,
        "minQualityImprovement": args.min_quality_improvement,
    }


def load_reviewed_report(
    *,
    output_path: Path,
    context: Context,
    candidates_root: Path,
    script_sha256: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    if not output_path.is_file():
        raise AudioQcError(
            f"reviewed comparison report not found: {output_path}; "
            "run compare without --apply first"
        )
    try:
        report = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioQcError(f"cannot read reviewed comparison report {output_path}: {exc}") from exc
    expected_script = display_path(context.script_path, context.repo_root)
    expected_candidates = display_path(candidates_root, context.repo_root)
    if report.get("schemaVersion") != SCHEMA_VERSION or report.get("kind") != "audio_qc_comparison":
        raise AudioQcError("comparison report is not a current audio_qc review; run dry-run again")
    if report.get("script") != expected_script or report.get("scriptSha256") != script_sha256:
        raise AudioQcError("script changed since comparison review; run dry-run again")
    if report.get("candidatesDir") != expected_candidates:
        raise AudioQcError("candidate directory differs from comparison review; run dry-run again")
    if report.get("settings") != settings:
        raise AudioQcError("QC settings differ from comparison review; run dry-run again")
    comparisons = report.get("comparisons")
    if not isinstance(comparisons, list):
        raise AudioQcError("comparison report has no review entries; run dry-run again")
    keys: set[tuple[str, str]] = set()
    for item in comparisons:
        if not isinstance(item, dict):
            raise AudioQcError("comparison report contains an invalid review entry")
        key = (str(item.get("id") or ""), str(item.get("speakerId") or ""))
        if not key[0] or key in keys:
            raise AudioQcError("comparison report contains missing or duplicate line IDs")
        keys.add(key)
        if item.get("applied"):
            raise AudioQcError("comparison report was already consumed; run dry-run again")
        for side in ("current", "candidate"):
            value = item.get(side)
            if not isinstance(value, dict) or "sha256" not in value:
                raise AudioQcError("comparison report is not hash-bound; run dry-run again")
    return report


def bind_comparisons_to_reviewed(
    comparisons: list[dict[str, Any]], reviewed_report: dict[str, Any]
) -> None:
    reviewed_by_key = {
        (str(item["id"]), str(item["speakerId"])): item
        for item in reviewed_report["comparisons"]
    }
    for comparison in comparisons:
        key = (str(comparison["id"]), str(comparison["speakerId"]))
        reviewed = reviewed_by_key.get(key)
        if reviewed is None:
            comparison["decision"] = "keep"
            comparison["reason"] = "line_not_present_in_reviewed_report"
            continue
        reviewed_decision = str(reviewed.get("decision") or "keep")
        comparison["reviewedDecision"] = reviewed_decision
        if reviewed_decision != "replace":
            comparison["decision"] = "keep"
            comparison["reason"] = "not_approved_by_reviewed_report"
            continue
        reviewed_current = reviewed["current"]
        reviewed_candidate = reviewed["candidate"]
        current = comparison["current"]
        candidate = comparison["candidate"]
        if reviewed_current.get("audio") != current.get("audio"):
            comparison["decision"] = "keep"
            comparison["reason"] = "current_path_changed_since_review"
        elif reviewed_candidate.get("audio") != candidate.get("audio"):
            comparison["decision"] = "keep"
            comparison["reason"] = "candidate_path_changed_since_review"
        elif reviewed_current.get("sha256") != current.get("sha256"):
            comparison["decision"] = "keep"
            comparison["reason"] = "current_changed_since_review"
        elif reviewed_candidate.get("sha256") != candidate.get("sha256"):
            comparison["decision"] = "keep"
            comparison["reason"] = "candidate_changed_since_review"


class Context:
    def __init__(self, *, repo_root: Path, script_path: Path, script: dict[str, Any], lines: list[dict[str, Any]]) -> None:
        self.repo_root = repo_root
        self.script_path = script_path
        self.script = script
        self.lines = lines


def load_context(args: argparse.Namespace) -> Context:
    repo_root = Path(args.repo_root).expanduser().resolve()
    script_path = resolve_cli_path(args.script, repo_root)
    if not script_path.is_file():
        raise AudioQcError(f"script not found: {script_path}")
    try:
        script = json.loads(script_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioQcError(f"cannot read script JSON {script_path}: {exc}") from exc
    if not isinstance(script, dict):
        raise AudioQcError(f"expected a JSON object: {script_path}")
    lines = flatten_lines(script)
    audio_lines = [line for line in lines if line.get("audio")]
    if not audio_lines:
        raise AudioQcError(f"script has no audio lines: {script_path}")
    return Context(repo_root=repo_root, script_path=script_path, script=script, lines=audio_lines)


def flatten_lines(script: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(script.get("lines"), list):
        return [dict(line) for line in script["lines"] if isinstance(line, dict)]
    lines: list[dict[str, Any]] = []
    scenes = script.get("scenes", [])
    if not isinstance(scenes, list):
        return lines
    for scene in scenes:
        if not isinstance(scene, dict) or not isinstance(scene.get("lines"), list):
            continue
        lines.extend(dict(line) for line in scene["lines"] if isinstance(line, dict))
    return lines


def analyze_script_audio(*, context: Context, args: argparse.Namespace) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, line in enumerate(context.lines, start=1):
        line_id = str(line.get("id") or f"line_{index:03d}")
        speaker_id = str(line.get("speakerId") or "")
        audio_label = str(line.get("audio") or "")
        audio_path = resolve_cli_path(audio_label, context.repo_root)
        entries.append(
            analyze_entry(
                line_id=line_id,
                index=index,
                speaker_id=speaker_id,
                audio_label=audio_label,
                audio_path=audio_path,
                args=args,
            )
        )
    return entries


def analyze_entry(
    *,
    line_id: str,
    index: int,
    speaker_id: str,
    audio_label: str,
    audio_path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": line_id,
        "index": index,
        "speakerId": speaker_id,
        "audio": audio_label,
        "resolvedPath": str(audio_path),
        "exists": audio_path.is_file(),
        "status": "error",
        "issues": [],
        "metrics": None,
        "qualityScore": None,
        "voiceSimilarity": None,
        "sha256": None,
    }
    if not speaker_id:
        entry["issues"].append(issue("missing_speaker", "error", "audio line has no speakerId", False))
    if audio_path.suffix.lower() != ".wav":
        entry["issues"].append(
            issue("unexpected_extension", "warning", "audio path does not end in .wav", False)
        )
    if not audio_path.is_file():
        entry["issues"].append(issue("missing_audio", "error", "audio file does not exist", True))
        return finalize_entry(entry)

    try:
        analyzed_sha256 = sha256_file(audio_path)
    except AudioQcError as exc:
        entry["issues"].append(issue("hash_failed", "error", str(exc), True))
        return finalize_entry(entry)
    entry["sha256"] = analyzed_sha256

    try:
        probe = probe_audio(audio_path, ffprobe=args.ffprobe)
    except AudioQcError as exc:
        entry["issues"].append(issue("probe_failed", "error", str(exc), True))
        return finalize_entry(entry)
    try:
        decoded = decode_metrics(
            audio_path,
            ffmpeg=args.ffmpeg,
            silence_dbfs=args.silence_dbfs,
        )
    except AudioQcError as exc:
        entry["issues"].append(issue("decode_failed", "error", str(exc), True))
        return finalize_entry(entry)

    metrics = {**probe, **decoded}
    entry["metrics"] = metrics
    try:
        final_sha256 = sha256_file(audio_path)
    except AudioQcError as exc:
        entry["issues"].append(issue("hash_failed", "error", str(exc), True))
        return finalize_entry(entry)
    if final_sha256 != analyzed_sha256:
        entry["sha256"] = final_sha256
        entry["issues"].append(
            issue(
                "changed_during_analysis",
                "error",
                "audio bytes changed while QC was running",
                True,
            )
        )
    duration = float(metrics["durationSec"])
    if duration < args.min_duration:
        entry["issues"].append(
            issue(
                "duration_too_short",
                "error",
                f"duration {duration:.3f}s is below {args.min_duration:.3f}s",
                True,
            )
        )
    if duration > args.max_duration:
        entry["issues"].append(
            issue(
                "duration_too_long",
                "error",
                f"duration {duration:.3f}s exceeds {args.max_duration:.3f}s",
                True,
            )
        )
    if args.expected_sample_rate and metrics["sampleRate"] != args.expected_sample_rate:
        entry["issues"].append(
            issue(
                "sample_rate_mismatch",
                "warning",
                f"sample rate is {metrics['sampleRate']} Hz, expected {args.expected_sample_rate} Hz",
                True,
            )
        )
    if args.expected_channels and metrics["channels"] != args.expected_channels:
        entry["issues"].append(
            issue(
                "channel_mismatch",
                "warning",
                f"channel count is {metrics['channels']}, expected {args.expected_channels}",
                True,
            )
        )
    if metrics["rmsDbfs"] is None or metrics["rmsDbfs"] < args.min_rms_dbfs:
        entry["issues"].append(
            issue(
                "low_rms",
                "warning",
                f"RMS is below {args.min_rms_dbfs:.1f} dBFS",
                True,
            )
        )
    if metrics["silentRatio"] > args.max_silent_ratio:
        entry["issues"].append(
            issue(
                "excessive_silence",
                "warning",
                f"silent sample ratio {metrics['silentRatio']:.4f} exceeds {args.max_silent_ratio:.4f}",
                True,
            )
        )
    if metrics["clippingRatio"] > args.max_clipping_ratio:
        entry["issues"].append(
            issue(
                "clipping",
                "warning",
                f"clipping ratio {metrics['clippingRatio']:.6f} exceeds {args.max_clipping_ratio:.6f}",
                True,
            )
        )
    if metrics["leadingSilenceSec"] > args.max_edge_silence:
        entry["issues"].append(
            issue(
                "long_leading_silence",
                "warning",
                f"leading silence {metrics['leadingSilenceSec']:.3f}s exceeds {args.max_edge_silence:.3f}s",
                True,
            )
        )
    if metrics["trailingSilenceSec"] > args.max_edge_silence:
        entry["issues"].append(
            issue(
                "long_trailing_silence",
                "warning",
                f"trailing silence {metrics['trailingSilenceSec']:.3f}s exceeds {args.max_edge_silence:.3f}s",
                True,
            )
        )
    entry["qualityScore"] = objective_quality_score(metrics=metrics, issues=entry["issues"])
    return finalize_entry(entry)


def probe_audio(path: Path, *, ffprobe: str) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise AudioQcError(f"cannot run {ffprobe}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise AudioQcError(f"ffprobe rejected {path.name}: {detail}")
    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        duration_raw = stream.get("duration") or payload.get("format", {}).get("duration")
        duration = float(duration_raw)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AudioQcError(f"ffprobe returned incomplete audio metadata for {path.name}") from exc
    if sample_rate <= 0 or channels <= 0 or not math.isfinite(duration) or duration <= 0:
        raise AudioQcError(f"ffprobe returned invalid audio metadata for {path.name}")
    return {
        "codec": str(stream.get("codec_name") or "unknown"),
        "sampleRate": sample_rate,
        "channels": channels,
        "durationSec": round(duration, 6),
    }


def decode_metrics(path: Path, *, ffmpeg: str, silence_dbfs: float) -> dict[str, Any]:
    command = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(DECODE_SAMPLE_RATE),
        "-f",
        "f32le",
        "pipe:1",
    ]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise AudioQcError(f"cannot run {ffmpeg}: {exc}") from exc
    assert process.stdout is not None
    total = 0
    sum_squares = 0.0
    peak = 0.0
    silent = 0
    clipped = 0
    nonfinite = 0
    leading_silent = 0
    last_nonsilent = -1
    threshold = 10.0 ** (silence_dbfs / 20.0)
    leftover = b""
    while True:
        chunk = process.stdout.read(262_144)
        if not chunk:
            break
        payload = leftover + chunk
        aligned = len(payload) - (len(payload) % 4)
        body, leftover = payload[:aligned], payload[aligned:]
        for (sample,) in struct.iter_unpack("<f", body):
            if not math.isfinite(sample):
                nonfinite += 1
                continue
            value = abs(float(sample))
            peak = max(peak, value)
            sum_squares += value * value
            is_silent = value <= threshold
            if is_silent:
                silent += 1
                if last_nonsilent < 0:
                    leading_silent += 1
            else:
                last_nonsilent = total
            if value >= 0.999:
                clipped += 1
            total += 1
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    returncode = process.wait()
    if returncode != 0:
        raise AudioQcError(f"ffmpeg could not decode {path.name}: {stderr.strip() or f'exit {returncode}'}")
    if leftover:
        raise AudioQcError(f"ffmpeg returned truncated PCM for {path.name}")
    if total <= 0:
        raise AudioQcError(f"ffmpeg decoded no samples from {path.name}")
    if nonfinite:
        raise AudioQcError(f"ffmpeg decoded {nonfinite} non-finite samples from {path.name}")
    trailing_silent = total if last_nonsilent < 0 else total - last_nonsilent - 1
    rms = math.sqrt(sum_squares / total)
    return {
        "decodedSampleRate": DECODE_SAMPLE_RATE,
        "decodedSamples": total,
        "peakDbfs": dbfs(peak),
        "rmsDbfs": dbfs(rms),
        "silentRatio": round(silent / total, 6),
        "clippingRatio": round(clipped / total, 6),
        "leadingSilenceSec": round(leading_silent / DECODE_SAMPLE_RATE, 6),
        "trailingSilenceSec": round(trailing_silent / DECODE_SAMPLE_RATE, 6),
    }


def dbfs(amplitude: float) -> float | None:
    if amplitude <= 0:
        return None
    return round(20.0 * math.log10(amplitude), 3)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AudioQcError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def issue(code: str, severity: str, message: str, retry: bool) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, "retryRecommended": retry}


def finalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    severities = {item["severity"] for item in entry["issues"]}
    if "error" in severities:
        entry["status"] = "error"
    elif "warning" in severities:
        entry["status"] = "review"
    else:
        entry["status"] = "pass"
    return entry


def objective_quality_score(*, metrics: dict[str, Any], issues: list[dict[str, Any]]) -> float:
    score = 100.0
    retry_codes = {item["code"] for item in issues if item.get("retryRecommended")}
    score -= 45.0 * min(1.0, float(metrics["clippingRatio"]) / 0.01)
    score -= 30.0 * max(0.0, (float(metrics["silentRatio"]) - 0.55) / 0.45)
    for key in ("leadingSilenceSec", "trailingSilenceSec"):
        score -= min(10.0, max(0.0, float(metrics[key]) - 0.25) * 8.0)
    rms = metrics.get("rmsDbfs")
    if rms is None:
        score -= 35.0
    elif rms < -35.0:
        score -= min(30.0, (-35.0 - float(rms)) * 1.5)
    if "duration_too_short" in retry_codes or "duration_too_long" in retry_codes:
        score -= 25.0
    return round(max(0.0, min(100.0, score)), 3)


def speaker_index(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        grouped[entry["speakerId"]].append(entry["id"])
    return [
        {
            "speakerId": speaker_id,
            "lineIds": line_ids,
            "referenceAudio": None,
            "roletoneStatus": "disabled",
            "error": None,
        }
        for speaker_id, line_ids in grouped.items()
    ]


def apply_roletone(
    *, entries: list[dict[str, Any]], context: Context, args: argparse.Namespace
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        if entry["speakerId"] and entry.get("resolvedPath") and entry["status"] != "error":
            grouped[entry["speakerId"]].append(entry)
    speakers: list[dict[str, Any]] = []
    all_speaker_ids = list(dict.fromkeys(entry["speakerId"] for entry in entries))
    characters_root = resolve_cli_path(args.characters_root, context.repo_root)
    scorer: Any | None = None
    backend_error: str | None = None
    if args.roletone_runner == "in-process":
        try:
            scorer = make_roletone_scorer(repo_root=context.repo_root, args=args)
            scorer.load()
        except Exception as exc:  # RoleTone wraps torch/transformers model-loading errors.
            backend_error = f"RoleTone model load failed: {exc}"
    for speaker_id in all_speaker_ids:
        speaker_entries = grouped.get(speaker_id, [])
        record = {
            "speakerId": speaker_id,
            "lineIds": list(
                dict.fromkeys(
                    entry["id"] for entry in entries if entry["speakerId"] == speaker_id
                )
            ),
            "referenceAudio": None,
            "roletoneStatus": "skipped",
            "error": None,
        }
        if not speaker_id:
            record["error"] = "missing speakerId"
            speakers.append(record)
            continue
        if backend_error:
            record["roletoneStatus"] = "error"
            record["error"] = backend_error
            mark_roletone_error(speaker_entries, backend_error, args=args)
            speakers.append(record)
            continue
        reference = resolve_reference_audio(characters_root=characters_root, speaker_id=speaker_id)
        if reference is None:
            record["roletoneStatus"] = "error"
            record["error"] = "reference.mp3 or reference.wav not found"
            mark_roletone_error(speaker_entries, record["error"], args=args)
            speakers.append(record)
            continue
        record["referenceAudio"] = display_path(reference, context.repo_root)
        if not speaker_entries:
            speakers.append(record)
            continue
        try:
            results = run_roletone(
                reference=reference,
                candidates=[Path(entry["resolvedPath"]) for entry in speaker_entries],
                repo_root=context.repo_root,
                args=args,
                scorer=scorer,
            )
            result_by_path = {str(Path(item["candidate"]).expanduser().resolve()): item for item in results}
            for entry in speaker_entries:
                result = result_by_path.get(str(Path(entry["resolvedPath"]).resolve()))
                if result is None:
                    raise AudioQcError(f"RoleTone omitted {Path(entry['resolvedPath']).name}")
                duration = float(entry["metrics"]["durationSec"])
                threshold = roletone_threshold(duration, args)
                score = float(result["score"])
                verdict = str(result.get("verdict") or "").strip().lower()
                strict_threshold = max(threshold, float(args.roletone_threshold_normal))
                mismatch = verdict == "mismatch"
                lenient_pass = duration < 2.0 and score >= threshold and score < strict_threshold
                passed = score >= strict_threshold and not mismatch
                entry["voiceSimilarity"] = {
                    "status": "pass" if passed else "review",
                    "score": round(score, 3),
                    "cosine": round(float(result["cosine"]), 6),
                    "threshold": threshold,
                    "autoPassThreshold": strict_threshold,
                    "passed": passed,
                    "lenientPass": lenient_pass and not mismatch,
                    "verdict": verdict,
                    "model": str(result.get("model") or args.roletone_model),
                }
                if mismatch:
                    entry["issues"].append(
                        issue(
                            "roletone_verdict_mismatch",
                            "warning",
                            "RoleTone classified the voice as a mismatch",
                            True,
                        )
                    )
                    finalize_entry(entry)
                elif lenient_pass:
                    entry["issues"].append(
                        issue(
                            "short_line_roletone_review",
                            "warning",
                            (
                                f"short-line score {score:.3f} passes the lenient threshold "
                                f"{threshold:.3f} but requires review below {strict_threshold:.3f}"
                            ),
                            False,
                        )
                    )
                    finalize_entry(entry)
                elif not passed:
                    entry["issues"].append(
                        issue(
                            "low_roletone_score",
                            "warning",
                            f"RoleTone score {score:.3f} is below duration-aware threshold {threshold:.3f}",
                            True,
                        )
                    )
                    finalize_entry(entry)
            record["roletoneStatus"] = "complete"
        except AudioQcError as exc:
            record["roletoneStatus"] = "error"
            record["error"] = str(exc)
            mark_roletone_error(speaker_entries, str(exc), args=args)
        speakers.append(record)
    return speakers


def run_roletone(
    *,
    reference: Path,
    candidates: list[Path],
    repo_root: Path,
    args: argparse.Namespace,
    scorer: Any | None = None,
) -> list[dict[str, Any]]:
    if scorer is not None:
        try:
            return [result.to_dict() for result in scorer.compare_many(reference, candidates)]
        except Exception as exc:  # Keep optional RoleTone failures inside the report.
            raise AudioQcError(f"RoleTone failed: {exc}") from exc

    executable = resolve_executable(args.roletone_cli, repo_root)
    command = [
        executable,
        "score",
        "--reference",
        str(reference),
    ]
    for candidate in candidates:
        command.extend(["--candidate", str(candidate)])
    command.extend(
        [
            "--format",
            "json",
            "--model",
            args.roletone_model,
            "--device",
            args.roletone_device,
            "--hf-home",
            str(resolve_cli_path(args.roletone_hf_home, repo_root)),
        ]
    )
    if not args.roletone_online:
        command.append("--offline")
    env = os.environ.copy()
    env["NUMBA_CACHE_DIR"] = str(resolve_cli_path(args.roletone_numba_cache, repo_root))
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise AudioQcError(f"cannot run RoleTone: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise AudioQcError(f"RoleTone failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AudioQcError("RoleTone did not return JSON") from exc
    if not isinstance(payload, list):
        raise AudioQcError("RoleTone JSON result is not a list")
    return [item for item in payload if isinstance(item, dict)]


def make_roletone_scorer(*, repo_root: Path, args: argparse.Namespace) -> Any:
    os.environ["NUMBA_CACHE_DIR"] = str(
        resolve_cli_path(args.roletone_numba_cache, repo_root)
    )
    try:
        from roletone.scorer import WavLMScorer
    except ImportError as exc:
        raise AudioQcError("RoleTone is not installed; run uv sync --extra all") from exc
    return WavLMScorer(
        model=args.roletone_model,
        device=args.roletone_device,
        hf_home=resolve_cli_path(args.roletone_hf_home, repo_root),
        local_files_only=not args.roletone_online,
    )


def mark_roletone_error(
    entries: Iterable[dict[str, Any]], message: str, *, args: argparse.Namespace
) -> None:
    for entry in entries:
        entry["voiceSimilarity"] = {
            "status": "error",
            "score": None,
            "cosine": None,
            "threshold": roletone_threshold(float(entry["metrics"]["durationSec"]), args),
            "passed": False,
            "verdict": "",
            "model": "",
            "error": message,
        }
        entry["issues"].append(
            issue("roletone_failed", "warning", "RoleTone scoring did not complete", False)
        )
        finalize_entry(entry)


def resolve_executable(value: str, repo_root: Path) -> str:
    candidate = Path(value).expanduser()
    if candidate.is_absolute() and candidate.is_file():
        return str(candidate)
    relative = repo_root / candidate
    if relative.is_file():
        return str(relative)
    discovered = shutil.which(value)
    if discovered:
        return discovered
    raise AudioQcError(f"RoleTone CLI not found: {value}")


def resolve_reference_audio(*, characters_root: Path, speaker_id: str) -> Path | None:
    character_dir = characters_root / speaker_id.removeprefix("uma_")
    for filename in ("reference.mp3", "reference.wav"):
        candidate = character_dir / filename
        if candidate.is_file():
            return candidate.resolve()
    return None


def roletone_threshold(duration_sec: float, args: argparse.Namespace) -> float:
    if duration_sec < 0.5:
        return float(args.roletone_threshold_very_short)
    if duration_sec < 1.0:
        return float(args.roletone_threshold_short)
    if duration_sec < 2.0:
        return float(args.roletone_threshold_medium)
    return float(args.roletone_threshold_normal)


def build_qc_report(
    *,
    context: Context,
    entries: list[dict[str, Any]],
    speakers: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    public = [public_entry(entry) for entry in entries]
    retry_count = sum(1 for entry in public if entry_needs_retry(entry))
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "audio_qc",
        "script": display_path(context.script_path, context.repo_root),
        "settings": {
            "expectedSampleRate": args.expected_sample_rate,
            "expectedChannels": args.expected_channels,
            "minDurationSec": args.min_duration,
            "maxDurationSec": args.max_duration,
            "silenceDbfs": args.silence_dbfs,
            "maxSilentRatio": args.max_silent_ratio,
            "minRmsDbfs": args.min_rms_dbfs,
            "maxClippingRatio": args.max_clipping_ratio,
            "maxEdgeSilenceSec": args.max_edge_silence,
            "roletoneEnabled": bool(args.roletone),
            "roletoneRunner": args.roletone_runner if args.roletone else None,
            "roletoneModel": args.roletone_model if args.roletone else None,
            "roletoneDevice": args.roletone_device if args.roletone else None,
            "roletoneOffline": bool(args.roletone and not args.roletone_online),
            "roletoneThresholds": {
                "under0.5s": args.roletone_threshold_very_short,
                "under1s": args.roletone_threshold_short,
                "under2s": args.roletone_threshold_medium,
                "normal": args.roletone_threshold_normal,
            },
        },
        "summary": {
            "total": len(public),
            "pass": sum(1 for entry in public if entry["status"] == "pass"),
            "review": sum(1 for entry in public if entry["status"] == "review"),
            "error": sum(1 for entry in public if entry["status"] == "error"),
            "retryRecommended": retry_count,
            "roletoneErrors": sum(
                1 for speaker in speakers if speaker["roletoneStatus"] == "error"
            ),
        },
        "speakers": speakers,
        "lines": public,
    }


def public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "index": entry["index"],
        "speakerId": entry["speakerId"],
        "audio": entry["audio"],
        "sha256": entry.get("sha256"),
        "exists": entry["exists"],
        "status": entry["status"],
        "qualityScore": entry["qualityScore"],
        "metrics": entry["metrics"],
        "voiceSimilarity": entry["voiceSimilarity"],
        "issues": entry["issues"],
    }


def build_retry_plan(*, context: Context, report: dict[str, Any]) -> dict[str, Any]:
    items = []
    for entry in report["lines"]:
        reasons = [item["code"] for item in entry["issues"] if item.get("retryRecommended")]
        if not reasons:
            continue
        items.append(
            {
                "id": entry["id"],
                "speakerId": entry["speakerId"],
                "audio": entry["audio"],
                "reasons": reasons,
                "qualityScore": entry["qualityScore"],
                "roletoneScore": (
                    entry["voiceSimilarity"]["score"] if entry.get("voiceSimilarity") else None
                ),
                "roletoneThreshold": (
                    entry["voiceSimilarity"]["threshold"] if entry.get("voiceSimilarity") else None
                ),
            }
        )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "audio_retry_plan",
        "script": display_path(context.script_path, context.repo_root),
        "count": len(items),
        "items": items,
    }


def entry_needs_retry(entry: dict[str, Any]) -> bool:
    return any(item.get("retryRecommended") for item in entry["issues"])


def find_candidate(*, candidates_root: Path, speaker_id: str, filename: str) -> tuple[Path | None, str | None]:
    direct = candidates_root / speaker_id / filename
    if direct.is_file():
        return direct.resolve(), None
    flat = candidates_root / filename
    if flat.is_file():
        return flat.resolve(), None
    matches = sorted(path.resolve() for path in candidates_root.rglob(filename) if path.is_file())
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "ambiguous_candidate"
    return None, "missing_candidate"


def replacement_decision(
    *,
    current: dict[str, Any],
    candidate: dict[str, Any],
    min_score_improvement: float,
    min_quality_improvement: float,
    require_voice_score: bool = False,
) -> tuple[str, str]:
    if candidate.get("status") == "missing":
        return "keep", "candidate_missing_or_invalid"
    objective_blockers = {
        str(item.get("code") or "")
        for item in candidate.get("issues", [])
        if str(item.get("code") or "") in OBJECTIVE_BLOCKING_CODES
    }
    if objective_blockers:
        return "keep", "candidate_failed_objective_qc"
    if candidate.get("status") == "error":
        return "keep", "candidate_missing_or_invalid"
    candidate_voice = candidate.get("voiceSimilarity")
    current_voice = current.get("voiceSimilarity")
    candidate_voice_available = candidate_voice and candidate_voice.get("score") is not None
    if require_voice_score and not candidate_voice_available:
        return "keep", "roletone_score_unavailable"
    if (
        require_voice_score
        and str(candidate_voice.get("verdict") or "").strip().lower() == "mismatch"
    ):
        return "keep", "candidate_roletone_verdict_mismatch"
    if require_voice_score and not candidate_voice.get("passed"):
        return "keep", "candidate_roletone_review_only"
    if current.get("status") == "error":
        candidate_quality = candidate.get("qualityScore")
        current_quality = current.get("qualityScore")
        if candidate_quality is None:
            return "keep", "candidate_has_no_quality_score"
        if current_quality is not None and float(candidate_quality) < float(current_quality):
            return "keep", "objective_quality_regressed"
        return "replace", "candidate_is_valid_and_current_is_invalid"
    candidate_quality = candidate.get("qualityScore")
    current_quality = current.get("qualityScore")
    if candidate_quality is None:
        return "keep", "candidate_has_no_quality_score"
    if current_quality is None:
        return "replace", "candidate_is_valid_and_current_has_no_quality_score"

    voice_scores_available = (
        candidate_voice
        and current_voice
        and candidate_voice.get("score") is not None
        and current_voice.get("score") is not None
    )
    if voice_scores_available:
        score_gain = float(candidate_voice["score"]) - float(current_voice["score"])
        if score_gain < min_score_improvement:
            return "keep", "roletone_score_did_not_improve_enough"
        if float(candidate_quality) < float(current_quality):
            return "keep", "objective_quality_regressed"
        return "replace", "roletone_score_improved_without_quality_regression"

    if require_voice_score:
        return "keep", "roletone_score_unavailable"

    quality_gain = float(candidate_quality) - float(current_quality)
    if quality_gain < min_quality_improvement:
        return "keep", "objective_quality_did_not_improve_enough"
    return "replace", "objective_quality_improved"


def apply_replacements(
    *,
    comparisons: list[dict[str, Any]],
    current_entries: list[dict[str, Any]],
    candidate_entries: list[dict[str, Any]],
    backup_root: Path,
    repo_root: Path,
) -> None:
    for comparison, current, candidate in zip(
        comparisons, current_entries, candidate_entries, strict=True
    ):
        if comparison["decision"] != "replace":
            continue
        current_path = Path(current["resolvedPath"])
        candidate_path = Path(candidate["resolvedPath"])
        if current_path.resolve() == candidate_path.resolve():
            comparison["decision"] = "keep"
            comparison["reason"] = "candidate_is_current_file"
            continue
        current_sha256 = str(current.get("sha256") or "")
        candidate_sha256 = str(candidate.get("sha256") or "")
        if not current_sha256 or not current_path.is_file() or sha256_file(current_path) != current_sha256:
            comparison["decision"] = "keep"
            comparison["reason"] = "current_changed_during_apply"
            continue
        if not candidate_sha256 or not candidate_path.is_file() or sha256_file(candidate_path) != candidate_sha256:
            comparison["decision"] = "keep"
            comparison["reason"] = "candidate_changed_during_apply"
            continue
        try:
            relative = current_path.resolve().relative_to(repo_root)
        except ValueError:
            relative = Path(current_path.name)
        backup = select_backup_path(backup_root / relative, current_sha256)
        atomic_copy_verified(current_path, backup, current_sha256)
        if sha256_file(current_path) != current_sha256:
            comparison["decision"] = "keep"
            comparison["reason"] = "current_changed_during_apply"
            continue
        current_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = verified_temporary_copy(
            source=candidate_path,
            destination_dir=current_path.parent,
            destination_name=current_path.name,
            expected_sha256=candidate_sha256,
        )
        try:
            if sha256_file(current_path) != current_sha256:
                comparison["decision"] = "keep"
                comparison["reason"] = "current_changed_during_apply"
                continue
            os.replace(temporary, current_path)
        finally:
            temporary.unlink(missing_ok=True)
        if sha256_file(current_path) != candidate_sha256:
            raise AudioQcError(f"replacement verification failed for {current_path}")
        comparison["applied"] = True
        comparison["backup"] = display_path(backup, repo_root)


def select_backup_path(base: Path, current_sha256: str) -> Path:
    if not base.exists() or sha256_file(base) == current_sha256:
        return base
    versioned = base.with_name(f"{base.stem}.{current_sha256}{base.suffix}")
    if versioned.exists() and sha256_file(versioned) != current_sha256:
        raise AudioQcError(f"content-addressed backup collision: {versioned}")
    return versioned


def atomic_copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    if destination.exists():
        if sha256_file(destination) != expected_sha256:
            raise AudioQcError(f"existing backup does not match reviewed audio: {destination}")
        return
    temporary = verified_temporary_copy(
        source=source,
        destination_dir=destination.parent,
        destination_name=destination.name,
        expected_sha256=expected_sha256,
    )
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    if sha256_file(destination) != expected_sha256:
        raise AudioQcError(f"backup verification failed: {destination}")


def verified_temporary_copy(
    *, source: Path, destination_dir: Path, destination_name: str, expected_sha256: str
) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_name}.", suffix=".audio-qc-partial", dir=destination_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        if sha256_file(temporary) != expected_sha256:
            raise AudioQcError(f"audio changed while it was being copied: {source}")
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def default_qc_output_dir(script_path: Path) -> Path:
    stem = script_path.stem
    if stem.endswith("_script"):
        stem = stem[: -len("_script")]
    return script_path.parent / f"{stem}_audio_qc"


def resolve_cli_path(value: str | os.PathLike[str], repo_root: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
