"""Repeatable production/review commands; run from the repository root."""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.workflow_io import digest, lines, read, sha256, write

ROOT = Path(__file__).resolve().parents[1]
EDIT_FIELDS = {"spokenText", "subtitleJa", "subtitleZh", "fishStyle", "fishStyleTag",
               "fishStyleSyntax", "fishSpeakerTag", "fishEmotion", "ttsStyle", "ttsEmotion"}


def run(command: list[str], *, cwd: Path = ROOT) -> None:
    print("+ " + shlex.join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def python_tool(name: str, *args: str) -> list[str]:
    return [sys.executable, "-u", str(ROOT / "scripts" / name), *map(str, args)]


def project_path(script: dict, project: str | None = None) -> Path:
    name = project or script.get("projectId")
    if not name or not re.fullmatch(r"[\w-]+", name):
        raise ValueError("Expected a safe projectId or --project")
    return ROOT / "my-video/public/content" / name


def seconds(value: str) -> float:
    parts = value.split(":")
    if len(parts) > 3:
        raise ValueError("Timestamp must be seconds, MM:SS or HH:MM:SS")
    numbers = [float(x) for x in parts]
    if any(not math.isfinite(x) or x < 0 for x in numbers) or any(x >= 60 for x in numbers[1:]):
        raise ValueError("Invalid timestamp")
    return sum(n * 60 ** i for i, n in enumerate(reversed(numbers)))


def locate(script_path: Path, args: argparse.Namespace) -> dict:
    from scripts.tts_manifest import receipt_path
    script = read(script_path)
    all_lines = lines(script)
    timeline_path = Path(args.timeline) if getattr(args, "timeline", None) else project_path(script, args.project) / "timeline.json"
    timeline = read(timeline_path) if timeline_path.exists() else {}
    timing = {x["id"]: x for x in timeline.get("text", []) if x.get("id")}
    at = seconds(args.at) * 1000 - args.intro_ms if args.at is not None else None
    requested = set(args.line_id or [])
    unknown = requested - {x["id"] for x in all_lines}
    if unknown:
        raise ValueError(f"Unknown line IDs: {sorted(unknown)}")
    if at is not None and not timeline:
        raise ValueError("Timestamp lookup needs the rendered project's timeline.json")
    qc_path = Path(args.qc_report) if getattr(args, "qc_report", None) else script_path.with_name(script_path.stem.removesuffix("_script") + "_audio_qc") / "qc_report.json"
    qc = {x["id"]: x for x in read(qc_path).get("lines", [])} if qc_path.exists() else {}
    result = []
    for index, line in enumerate(all_lines):
        t = timing.get(line["id"])
        if requested and line["id"] not in requested:
            continue
        if args.speaker_id and line.get("speakerId") not in args.speaker_id:
            continue
        if args.text and args.text.casefold() not in "\n".join(str(line.get(k, "")) for k in ("spokenText", "subtitleJa", "subtitleZh")).casefold():
            continue
        # Include the following silent gap in the preceding line's lookup interval.
        next_t = timing.get(all_lines[index + 1]["id"]) if index + 1 < len(all_lines) else None
        end = next_t["startMs"] if next_t else (timeline.get("elements") or [{}])[-1].get("endMs")
        if at is not None and (not t or not t["startMs"] <= at < (end or t["endMs"])):
            continue
        audio = Path(line["audio"]).resolve() if line.get("audio") else None
        entry = qc.get(line["id"])
        qc_fresh = bool(entry and audio and audio.is_file() and entry.get("sha256") == sha256(audio))
        receipt = read(receipt_path(audio)) if audio and receipt_path(audio).exists() else None
        result.append({"index": index + 1, **line, "audioResolved": str(audio) if audio else None,
                       "videoStartMs": t["startMs"] + args.intro_ms if t else None,
                       "videoEndMs": t["endMs"] + args.intro_ms if t else None,
                       "inGap": bool(t and at is not None and at >= t["endMs"]),
                       "qc": entry, "qcMatchesAudio": qc_fresh, "ttsReceipt": receipt,
                       "receiptMatchesAudio": bool(receipt and audio.is_file() and receipt.get("outputSha256") == sha256(audio)),
                       "scriptSubtitlesMatchTimeline": bool(t and all(line.get(k, "") == t.get(k, "") for k in ("subtitleJa", "subtitleZh")))})
    return {"script": str(script_path), "timeline": str(timeline_path), "introMs": args.intro_ms,
            "note": "Timestamps refer to this timeline; a different/older MP4 needs its matching timeline.",
            "lines": result}


def selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--line-id", action="append")
    parser.add_argument("--speaker-id", action="append")
    parser.add_argument("--text")
    parser.add_argument("--at", help="Timestamp in the finished MP4, including the title intro")
    parser.add_argument("--intro-ms", type=int, default=1000)
    parser.add_argument("--project")
    parser.add_argument("--timeline", help="Optional matching timeline snapshot for an older MP4")
    parser.add_argument("--qc-report", help="Optional QC report, e.g. from the workflow's .work directory")


def repair_create(args: argparse.Namespace) -> None:
    from scripts.tts_manifest import receipt_path, request_fingerprint
    from scripts.synthesize_script import build_parser as tts_parser
    if not any((args.line_id, args.speaker_id, args.text, args.at)):
        raise ValueError("Select lines explicitly with --line-id/--speaker-id/--text/--at")
    source = Path(args.script).resolve()
    selected = locate(source, args)["lines"]
    ids = {x["id"] for x in selected}
    original = read(source)
    selected = [copy.deepcopy(x) for x in lines(original) if x["id"] in ids and x.get("audio")]
    if not selected:
        raise ValueError("No voiced lines matched")
    patch = read(Path(args.patch)) if args.patch else {}
    if set(patch) - ids:
        raise ValueError("Patch contains unselected IDs")
    for line in selected:
        edits = patch.get(line["id"], {})
        if not isinstance(edits, dict) or set(edits) - EDIT_FIELDS:
            raise ValueError(f"Unsupported edit for {line['id']}; IDs, paths and staging cannot be changed here")
        line.update(edits)
        line["audio"] = str(Path(line["audio"]).resolve())
    names = [Path(x["audio"]).name for x in selected]
    if len(names) != len(set(names)):
        raise ValueError("Selected WAV basenames must be unique for QC candidate matching")
    work = Path(args.work_dir).resolve()
    if work.exists():
        raise ValueError("Repair directory already exists; use its next stage or choose a fresh directory")
    work.mkdir(parents=True)
    compare_script = {"projectId": original.get("projectId"), "title": original.get("title"), "lines": selected}
    write(work / "review_script.json", compare_script)
    candidates = copy.deepcopy(compare_script)
    for line in candidates["lines"]:
        line["audio"] = str(work / "candidates" / Path(line["audio"]).name)
    write(work / "candidate_script.json", candidates)
    receipts = {}
    baselines = {}
    config_args = None
    if getattr(args, "config", None):
        config = read(Path(args.config))
        if Path(config["script"]).resolve() != source:
            raise ValueError("Repair config belongs to a different script")
        config_args = tts_parser().parse_args(tts_options(config))
    for line in selected:
        receipt_file = receipt_path(Path(line["audio"]))
        old = read(receipt_file) if receipt_file.exists() else {}
        if old.get("outputSha256") == sha256(Path(line["audio"])):
            receipts[line["id"]] = old
        settings = (old.get("acceptedSettings") or old.get("settings")) if line["id"] in receipts else None
        baseline_args = config_args or (argparse.Namespace(**settings) if settings else None)
        if baseline_args:
            baselines[line["id"]] = {"request": request_fingerprint(line, baseline_args), "settings": vars(baseline_args)}
    write(work / "repair.json", {"source": str(source), "sourceSha256": sha256(source), "patch": patch,
                                "reviewSha256": sha256(work / "review_script.json"),
                                "candidateSha256": sha256(work / "candidate_script.json"),
                                "originalAudio": {x["id"]: sha256(Path(x["audio"])) for x in selected},
                                "originalReceipts": receipts, "baselines": baselines})
    print(f"Prepared {len(selected)} lines in {work}; original script and WAVs unchanged")


def repair_stage(args: argparse.Namespace) -> None:
    from scripts.synthesize_script import build_parser as tts_parser
    from scripts.tts_manifest import receipt_path, request_fingerprint
    work = Path(args.work_dir).resolve()
    state = read(work / "repair.json")
    source = Path(state["source"])
    if state.get("completed"):
        raise ValueError("This repair has already been applied; create a new repair for another attempt")
    for path, expected in ((source, state["sourceSha256"]), (work / "review_script.json", state["reviewSha256"]),
                           (work / "candidate_script.json", state["candidateSha256"])):
        if sha256(path) != expected:
            raise ValueError(f"Repair inputs changed: {path}; create a new repair")
    if args.action == "generate":
        extra = args.tts_args or []
        forbidden = {"--script", "--overwrite", "--line-id", "--speaker-id"}
        if any(x.split("=")[0] in forbidden for x in extra):
            raise ValueError("Selection/output are fixed by repair-create; do not override them")
        generation = tts_parser().parse_args(["--script", str(work / "candidate_script.json"), *extra])
        state["charactersRoot"] = str(Path(generation.characters_root).resolve())
        write(work / "repair.json", state)
        run(python_tool("synthesize_script.py", "--script", work / "candidate_script.json", *extra))
        return
    for line in lines(read(work / "review_script.json")):
        baseline = state.get("baselines", {}).get(line["id"])
        if baseline and request_fingerprint(line, argparse.Namespace(**baseline["settings"])) != baseline["request"]:
            raise ValueError(f"Reference or TTS client changed during repair: {line['id']}")
    for line in lines(read(work / "candidate_script.json")):
        path = Path(line["audio"])
        receipt = receipt_path(path)
        if receipt.exists() and (not path.exists() or read(receipt).get("outputSha256") != sha256(path)):
            raise ValueError(f"Candidate WAV changed since generation: {line['id']}")
    report_path = work / "candidates/comparison_report.json"
    existing = read(report_path) if report_path.exists() else {}
    recovering = args.action == "apply" and any(x.get("applied") for x in existing.get("comparisons", []))
    if not recovering:
        for line in lines(read(work / "review_script.json")):
            if sha256(Path(line["audio"])) != state["originalAudio"][line["id"]]:
                raise ValueError(f"Original WAV changed: {line['id']}")
        command = python_tool("audio_qc.py", "compare", "--script", work / "review_script.json",
                              "--candidates-dir", work / "candidates", "--roletone",
                              "--characters-root", state.get("charactersRoot", str(ROOT / "characters")))
        if args.action == "apply":
            command.append("--apply")
        run(command)
    if args.action != "apply":
        print(f"Review {report_path} and listen before running repair apply")
        return
    report = read(report_path)
    applied = {x["id"]: x for x in report.get("comparisons", []) if x.get("applied")}
    script = read(source)
    write(work / "source_before_apply.json", script)
    candidate_lines = {x["id"]: x for x in lines(read(work / "candidate_script.json"))}
    for line in lines(script):
        if line["id"] not in applied:
            continue
        candidate = Path(candidate_lines[line["id"]]["audio"])
        if sha256(Path(line["audio"])) != applied[line["id"]]["candidate"]["sha256"]:
            raise ValueError("Applied WAV changed before finalization")
        line.update(state["patch"].get(line["id"], {}))
        from scripts.tts_manifest import copy_receipt
        baseline = state.get("baselines", {}).get(line["id"], {})
        copy_receipt(candidate, Path(line["audio"]), accepted_request=baseline.get("request"),
                     accepted_settings=baseline.get("settings"))
        old_receipt = state.get("originalReceipts", {}).get(line["id"])
        if old_receipt and applied[line["id"]].get("backup"):
            write(receipt_path(ROOT / applied[line["id"]]["backup"]), old_receipt)
    if applied:
        write(source, script)
    state.update(completed=True, applied=sorted(applied))
    write(work / "repair.json", state)
    print(f"Applied {len(applied)} lines; originals backed up. Rerun QC, Director and preview before render.")


def subtitle_check(script: Path, output: Path, project: str | None = None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    timeline = project_path(read(script), project) / "timeline.json"
    run(["pnpm", "exec", "tsx", "cli/subtitle-preflight.tsx", str(timeline), str(output.resolve())], cwd=ROOT / "my-video")


def preview(script: Path, output: Path, ids: list[str] | None, project: str | None = None) -> dict:
    content = project_path(read(script), project)
    timeline = read(content / "timeline.json")
    items = timeline["text"]
    known = {x["id"] for x in items}
    if ids and set(ids) - known:
        raise ValueError(f"Unknown preview IDs: {set(ids) - known}")
    if not ids:
        ids = [items[0]["id"], items[len(items) // 2]["id"], items[-1]["id"]]
    output.mkdir(parents=True, exist_ok=True)
    plan = []
    for item in items:
        if item["id"] not in ids:
            continue
        if not re.fullmatch(r"[\w-]+", item["id"]):
            raise ValueError("Preview IDs must be safe filenames")
        # Same 60fps metadata/1s intro as the final render, near the steady middle.
        frame = math.floor(60 + (item["startMs"] + item["endMs"]) * 0.03)
        frame = min(frame, math.ceil(60 + item["endMs"] * 0.06) - 1)
        plan.append({"id": item["id"], "frame": frame, "output": str((output / f"{item['id']}.png").resolve())})
    with tempfile.TemporaryDirectory(prefix="uma-preview-") as tmp:
        scratch = Path(tmp)
        public = scratch / "public/content"
        public.mkdir(parents=True)
        (public / content.name).symlink_to(content, target_is_directory=True)
        bundle = scratch / "bundle"
        plan_path = scratch / "plan.json"
        write(plan_path, plan)
        run(["pnpm", "exec", "remotion", "bundle", "src/index.ts", f"--public-dir={public.parent}",
             f"--out-dir={bundle}", "--disable-git-source"], cwd=ROOT / "my-video")
        composition = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff-]", "-", content.name)
        run(["pnpm", "exec", "tsx", "cli/preview-lines.ts", str(bundle), composition, str(plan_path)], cwd=ROOT / "my-video")
    result = {"timelineSha256": sha256(content / "timeline.json"), "stills": plan}
    write(output / "preview.json", result)
    return result


def tts_options(config: dict) -> list[str]:
    tts = config["tts"]
    extra = tts.get("args", [])
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        raise ValueError("tts.args must be an array of CLI arguments")
    reserved = {"--script", "--tts-engine", "--fish-tts-url", "--qwen3tts-url", "--model-id",
                "--overwrite", "--line-id", "--speaker-id", "--audit", "--audit-output"}
    if any(x.split("=")[0] in reserved for x in extra):
        raise ValueError("tts.args cannot override inputs/backend or force whole-project overwrites")
    if tts["engine"] not in ("fishspeech", "qwen3tts") or not tts.get("modelId"):
        raise ValueError("Set tts.engine and an explicit tts.modelId")
    return ["--script", config["script"], "--tts-engine", tts["engine"],
            "--fish-tts-url" if tts["engine"] == "fishspeech" else "--qwen3tts-url", tts["url"],
            "--model-id", tts["modelId"], *extra]


def audio_key(config: dict) -> str:
    from scripts.synthesize_script import build_parser as tts_parser
    from scripts.tts_manifest import request_fingerprint
    args = tts_parser().parse_args(tts_options(config))
    script = Path(config["script"])
    return digest({"tts": config["tts"], "audio": [
        {"id": x["id"], "sha": sha256(Path(x["audio"])), "request": request_fingerprint(x, args)}
        for x in lines(read(script)) if x.get("audio")]})


def visual_key(config: dict) -> str:
    content = project_path(read(Path(config["script"])), config.get("project"))
    # Bind review to the exact assets and renderer, not just the subtitle strings.
    paths = [content / "timeline.json"] + sorted((content / "images").glob("*.png"))
    paths += sorted((content / "audio").glob("*.mp3"))
    paths += [x for x in sorted((ROOT / "my-video/src").rglob("*")) if x.is_file()]
    paths += [ROOT / "my-video" / x for x in ("remotion.config.ts", "package.json", "pnpm-lock.yaml")]
    return digest({str(x): sha256(x) for x in paths})


def workflow(args: argparse.Namespace) -> None:
    from scripts.synthesize_script import build_parser as tts_parser
    config_path = Path(args.config).resolve()
    config = read(config_path)
    options = tts_options(config)
    characters = tts_parser().parse_args(options).characters_root
    script = Path(config["script"]).resolve()
    project = project_path(read(script), config.get("project")).name
    folder = config_path.with_suffix(".work")
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "state.json"
    state = read(state_path) if state_path.exists() else {}
    qc = folder / "audio_qc/qc_report.json"
    if args.command == "approve":
        current = audio_key(config)
        if state.get("audioKey") != current or not qc.exists() or state.get("qcHash") != sha256(qc):
            raise ValueError("Audio/QC changed or was never checked; run the workflow first")
        if args.stage == "audio":
            report = read(qc)
            if report["summary"].get("error") or report["summary"].get("roletoneErrors"):
                raise ValueError("Audio errors or failed RoleTone scoring must be resolved before approval")
            if report["summary"].get("review") and not args.accept_warnings:
                raise ValueError("Review short/low-score lines; use --accept-warnings only after listening")
            state["audioApproved"] = current
        else:
            key = visual_key(config)
            if state.get("audioApproved") != current or state.get("visualKey") != key:
                raise ValueError("Audio/visuals changed or previews are missing; rerun workflow")
            if not visual_reports_match(folder, state):
                raise ValueError("Subtitle report or previews changed; rerun workflow")
            subtitles = read(folder / "subtitles.json")
            if subtitles.get("flagged") and not args.accept_warnings:
                raise ValueError("Inspect flagged subtitles; use --accept-warnings only after visual review")
            state["visualApproved"] = key
        write(state_path, state)
        print(f"Recorded {args.stage} review for these exact inputs")
        return
    run(python_tool("synthesize_script.py", *options))
    current = audio_key(config)
    if state.get("audioKey") != current or not qc.exists() or state.get("qcHash") != sha256(qc):
        pending = qc.with_name(f".pending-{uuid.uuid4().hex}.json")
        command = python_tool("audio_qc.py", "check", "--script", script, "--output-dir", folder / "audio_qc",
                              "--report", pending, "--roletone", "--characters-root", characters)
        try:
            print("+ " + shlex.join(command), flush=True)
            result = subprocess.run(command, cwd=ROOT)
            if result.returncode not in (0, 1) or not pending.exists():
                raise ValueError("Audio QC failed to produce a new report")
            if audio_key(config) != current:
                raise ValueError("Audio inputs changed during QC; rerun")
            write(qc, read(pending))
        finally:
            pending.unlink(missing_ok=True)
        state.update(audioKey=current, qcHash=sha256(qc))
        write(state_path, state)
    if args.until == "audio" or state.get("audioApproved") != current:
        print(f"Audio review gate: inspect/listen using {qc}, then approve --stage audio")
        return
    run(python_tool("director.py", "build", "--script", script, "--project", project,
                    "--width", "3840", "--height", "2160", "--line-gap-ms", "100", "--characters-root", characters))
    key = visual_key(config)
    if state.get("visualKey") != key or not visual_reports_match(folder, state):
        subtitle_check(script, folder / "subtitles.json", project)
        timeline = read(project_path(read(script), project) / "timeline.json")
        items = timeline["text"]
        ids = [items[0]["id"], items[len(items)//2]["id"], items[-1]["id"]]
        previous_layout = None
        for line in lines(read(script)):
            layout = digest({k: line.get(k) for k in ("background", "characters")})
            if layout != previous_layout and line["id"] in {item["id"] for item in items}:
                ids.append(line["id"])
            previous_layout = layout
        ids += read(folder / "subtitles.json").get("flagged", [])
        preview(script, folder / "stills", ids, project)
        if visual_key(config) != key:
            raise ValueError("Visual inputs changed during preview; rerun")
        state["visualKey"] = key
        state["visualReports"] = {str(path): sha256(path) for path in
            [folder / "subtitles.json", folder / "stills/preview.json", *sorted((folder / "stills").glob("*.png"))]}
        state.pop("visualApproved", None)
        write(state_path, state)
    if args.until != "render" or state.get("visualApproved") != key:
        print(f"Visual review gate: inspect {folder / 'stills'} and subtitles.json, then approve --stage visual")
        return
    # The authoritative memory/backend-shutdown/disk gates remain in this wrapper.
    run([str(ROOT / "scripts/render_4k60.sh"), "--project", project,
         "--tts-engine", config["tts"]["engine"], "--tts-url", config["tts"]["url"]])


def visual_reports_match(folder: Path, state: dict) -> bool:
    expected = state.get("visualReports", {})
    return bool(expected and str(folder / "subtitles.json") in expected and
                all(Path(path).is_file() and sha256(Path(path)) == value for path, value in expected.items()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    loc = sub.add_parser("locate", help="Find lines using final video timestamps, text or stable IDs")
    loc.add_argument("--script", required=True)
    selection_args(loc)
    loc.add_argument("--output")
    loc.add_argument("--video", help="Optional matching MP4 for a listening excerpt")
    loc.add_argument("--clip", help="Save a single matched line plus context as WAV; never overwrite")
    loc.add_argument("--context-ms", type=int, default=1500)
    new_id = sub.add_parser("new-id", help="Allocate an insertion ID without renumbering any existing line")
    new_id.add_argument("--prefix", default="line")
    repair = sub.add_parser("repair", help="Candidate-only, reviewed selective audio repair")
    repair_sub = repair.add_subparsers(dest="action", required=True)
    create = repair_sub.add_parser("create")
    create.add_argument("--script", required=True)
    create.add_argument("--work-dir", required=True)
    create.add_argument("--patch", help="JSON mapping stable line IDs to spokenText/style/subtitle edits")
    create.add_argument("--config", help="Production config: bind reviewed retries to the original settings (especially for legacy audio)")
    selection_args(create)
    for name in ("generate", "compare", "apply"):
        stage = repair_sub.add_parser(name)
        stage.add_argument("--work-dir", required=True)
        if name == "generate":
            stage.add_argument("tts_args", nargs=argparse.REMAINDER, help="After --, pass synthesize_script flags")
    for name in ("subtitles", "preview"):
        check = sub.add_parser(name)
        check.add_argument("--script", required=True)
        check.add_argument("--project")
        check.add_argument("--output", required=True, help="Report file for subtitles; directory for preview")
        if name == "preview":
            check.add_argument("--line-id", action="append")
    init = sub.add_parser("init", help="Create a disposable pipeline parameter file, not a new script protocol")
    init.add_argument("--script", required=True)
    init.add_argument("--output", required=True)
    init.add_argument("--tts-engine", required=True, choices=("fishspeech", "qwen3tts"))
    init.add_argument("--tts-url", required=True)
    init.add_argument("--model-id", required=True)
    for name in ("run", "approve"):
        stage = sub.add_parser(name)
        stage.add_argument("--config", required=True)
        if name == "run":
            stage.add_argument("--until", choices=("audio", "preview", "render"), default="preview")
        else:
            stage.add_argument("--stage", choices=("audio", "visual"), required=True)
            stage.add_argument("--accept-warnings", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "locate":
            result = locate(Path(args.script).resolve(), args)
            if args.output:
                write(Path(args.output), result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.clip:
                if not args.video or len(result["lines"]) != 1 or args.context_ms < 0:
                    raise ValueError("--clip needs --video, exactly one match and nonnegative context")
                item = result["lines"][0]
                if item["videoStartMs"] is None:
                    raise ValueError("No timing for this clip")
                start = max(0, item["videoStartMs"] - args.context_ms)
                duration = item["videoEndMs"] + args.context_ms - start
                run(["ffmpeg", "-n", "-v", "error", "-ss", str(start / 1000), "-i", str(Path(args.video).resolve()),
                     "-t", str(duration / 1000), "-vn", "-c:a", "pcm_s16le", str(Path(args.clip).resolve())])
        elif args.command == "new-id":
            if not re.fullmatch(r"[\w-]+", args.prefix):
                raise ValueError("Unsafe ID prefix")
            print(f"{args.prefix}_{uuid.uuid4().hex[:12]}")
        elif args.command == "repair":
            if args.action == "create":
                repair_create(args)
            else:
                if getattr(args, "tts_args", None) and args.tts_args[0] == "--":
                    args.tts_args = args.tts_args[1:]
                repair_stage(args)
        elif args.command == "subtitles":
            subtitle_check(Path(args.script), Path(args.output), args.project)
        elif args.command == "preview":
            preview(Path(args.script), Path(args.output), args.line_id, args.project)
        elif args.command == "init":
            path = Path(args.output)
            if path.exists():
                raise ValueError("Config already exists")
            script = Path(args.script).resolve()
            lines(read(script))
            write(path, {"script": str(script), "tts": {"engine": args.tts_engine, "url": args.tts_url,
                "modelId": args.model_id, "args": ["--timeout", "900", "--batch-size", "4", "--temperature", "0.7",
                                                  "--top-p", "0.8", "--max-new-tokens", "512"]}})
        elif args.command in ("run", "approve"):
            workflow(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
