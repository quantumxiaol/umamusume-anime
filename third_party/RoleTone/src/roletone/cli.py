from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Sequence

from .scorer import ScoreResult, WavLMScorer, available_devices


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roletone",
        description="Score cloned TTS audio similarity against a reference voice using WavLM.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices = subparsers.add_parser("devices", help="Show available torch compute devices.")
    devices.set_defaults(func=cmd_devices)

    download = subparsers.add_parser("download", help="Download and cache the configured model.")
    add_model_args(download)
    download.set_defaults(func=cmd_download)

    score = subparsers.add_parser("score", help="Score one or more candidate audio files.")
    add_model_args(score)
    add_output_args(score)
    score.add_argument("-r", "--reference", required=True, help="Reference audio used for voice cloning.")
    score.add_argument(
        "-c",
        "--candidate",
        action="append",
        required=True,
        help="Generated candidate audio. Repeat for multiple files.",
    )
    score.set_defaults(func=cmd_score)

    score_dir = subparsers.add_parser("score-dir", help="Score all matching files in a directory.")
    add_model_args(score_dir)
    add_output_args(score_dir, default_format="csv")
    score_dir.add_argument("-r", "--reference", required=True, help="Reference audio used for voice cloning.")
    score_dir.add_argument("-d", "--candidates-dir", required=True, help="Directory containing generated audio.")
    score_dir.add_argument("--pattern", default="*.wav", help="Glob pattern, default: *.wav")
    score_dir.add_argument("--recursive", action="store_true", help="Search candidates recursively.")
    score_dir.set_defaults(func=cmd_score_dir)

    serve = subparsers.add_parser("serve", help="Start the FastAPI scoring service.")
    add_model_args(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    return parser


def add_model_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default=None,
        help="Model alias, HuggingFace id, or local model directory. Aliases: sv, base, base-plus, large.",
    )
    parser.add_argument(
        "--embedding-mode",
        choices=["auto", "xvector", "pooled"],
        default=None,
        help="auto: xvector for *-sv, pooled for base/large.",
    )
    parser.add_argument("--device", default=None, help="auto, cpu, cuda, mps, ...")
    parser.add_argument("--hf-home", default=None, help="HuggingFace cache root, e.g. ./modelsweights/huggingface.")
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Optional transformers cache_dir passed to from_pretrained.",
    )
    parser.add_argument(
        "--offline",
        "--local-files-only",
        action="store_true",
        dest="local_files_only",
        help="Load only local/cached model files; do not access HuggingFace Hub.",
    )


def add_output_args(parser: argparse.ArgumentParser, *, default_format: str = "text") -> None:
    parser.add_argument("--format", choices=["text", "json", "csv"], default=default_format)
    parser.add_argument("-o", "--output", default=None, help="Write results to this file.")


def cmd_download(args: argparse.Namespace) -> None:
    scorer = make_scorer(args)
    scorer.load()
    print(json.dumps(scorer.model_info(), ensure_ascii=False, indent=2))


def cmd_devices(args: argparse.Namespace) -> None:
    print(json.dumps(available_devices(), ensure_ascii=False, indent=2))


def cmd_score(args: argparse.Namespace) -> None:
    scorer = make_scorer(args)
    results = scorer.compare_many(args.reference, args.candidate)
    write_results(results, fmt=args.format, output=args.output)


def cmd_score_dir(args: argparse.Namespace) -> None:
    directory = Path(args.candidates_dir)
    if not directory.exists():
        raise FileNotFoundError(f"candidates directory not found: {directory}")
    globber = directory.rglob if args.recursive else directory.glob
    candidates = sorted(path for path in globber(args.pattern) if path.is_file())
    if not candidates:
        raise FileNotFoundError(f"no files matched {args.pattern!r} under {directory}")

    scorer = make_scorer(args)
    results = scorer.compare_many(args.reference, candidates)
    write_results(results, fmt=args.format, output=args.output)


def cmd_serve(args: argparse.Namespace) -> None:
    apply_runtime_env(args)

    import uvicorn

    uvicorn.run("roletone.api:app", host=args.host, port=args.port, reload=args.reload)


def apply_runtime_env(args: argparse.Namespace) -> None:
    if args.model:
        os.environ["ROLETONE_MODEL"] = args.model
    if args.embedding_mode:
        os.environ["ROLETONE_EMBEDDING_MODE"] = args.embedding_mode
    if args.device:
        os.environ["ROLETONE_DEVICE"] = args.device
    if args.hf_home:
        os.environ["HF_HOME"] = args.hf_home
    if args.cache_dir:
        os.environ["ROLETONE_CACHE_DIR"] = args.cache_dir
    if args.local_files_only:
        os.environ["ROLETONE_LOCAL_FILES_ONLY"] = "1"
        os.environ["HF_HUB_OFFLINE"] = "1"


def make_scorer(args: argparse.Namespace) -> WavLMScorer:
    return WavLMScorer(
        model=getattr(args, "model", None),
        embedding_mode=getattr(args, "embedding_mode", None),
        device=getattr(args, "device", None),
        hf_home=getattr(args, "hf_home", None),
        cache_dir=getattr(args, "cache_dir", None),
        local_files_only=getattr(args, "local_files_only", None) or None,
    )


def write_results(results: list[ScoreResult], *, fmt: str, output: str | None) -> None:
    if fmt == "json":
        content = json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2)
    elif fmt == "csv":
        content = results_to_csv(results)
    else:
        content = results_to_text(results)

    if output:
        Path(output).write_text(content + ("\n" if not content.endswith("\n") else ""), encoding="utf-8")
    else:
        print(content)


def results_to_text(results: list[ScoreResult]) -> str:
    lines = []
    for result in results:
        lines.append(
            f"{result.candidate}\t"
            f"cosine={result.cosine:.6f}\t"
            f"score={result.score:.3f}\t"
            f"verdict={result.verdict}"
        )
    return "\n".join(lines)


def results_to_csv(results: list[ScoreResult]) -> str:
    if not results:
        return ""
    fieldnames = list(results[0].to_dict().keys())
    from io import StringIO

    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for result in results:
        writer.writerow(result.to_dict())
    return buffer.getvalue().rstrip("\n")
