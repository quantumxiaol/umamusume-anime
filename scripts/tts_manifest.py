"""Per-WAV generation receipts. Missing history is not evidence of validity."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scripts.workflow_io import digest, read, sha256, write


def receipt_path(audio: Path) -> Path:
    return audio.with_name(audio.name + ".tts.json")


def request_fingerprint(line: dict, args) -> dict:
    # Import lazily to keep synthesis and manifest helpers independently usable.
    from scripts.synthesize_script import (fish_line_spoken_text, line_spoken_text,
                                           resolve_character_dir, resolve_reference_audio)
    from my_tts.cli import extract_fish_gen_kwargs, extract_qwen_gen_kwargs, format_fish_speech_text
    character = resolve_character_dir(characters_root=Path(args.characters_root), speaker_id=line["speakerId"])
    reference = resolve_reference_audio(character_dir=character, speaker_id=line["speakerId"])
    ref_text = (character / "reference_jp.txt").read_text(encoding="utf-8").strip()
    if not ref_text:
        raise ValueError(f"Empty reference text: {character}")
    fish = args.tts_engine == "fishspeech"
    params = extract_fish_gen_kwargs(args) if fish else extract_qwen_gen_kwargs(args)
    text = fish_line_spoken_text(line, args=args) if fish else line_spoken_text(line)
    if fish:
        # Match the client's final formatting pass, including per-line and global tags.
        text = format_fish_speech_text(text, **{k: params[k] for k in
            ("style", "style_tag", "style_syntax", "speaker_tag") if k in params})
    return {"schemaVersion": 1, "speakerId": line["speakerId"], "text": text,
            "referenceAudioSha256": sha256(reference), "referenceText": ref_text,
            "referenceTextSha256": digest(ref_text), "engine": args.tts_engine,
            "modelId": getattr(args, "model_id", None), "modelIdentitySource": "operator",
            "parameters": params, "format": args.fish_format if fish else "wav",
            "language": None if fish else args.language,
            "requestMode": "single" if args.no_batch else "batch", "batchSize": None if args.no_batch else args.batch_size,
            "clientSha256": sha256(Path(__file__).resolve().parents[1] / "my_tts/cli.py"),
            "referencePreparation": "source" if fish else "transcode_to_wav-v1"}


def cache_status(audio: Path, request: dict) -> str:
    if not audio.is_file():
        return "missing"
    path = receipt_path(audio)
    if not path.exists():
        return "unverified"
    try:
        receipt = read(path)
        if receipt.get("outputSha256") != sha256(audio):
            return "modified"
        if receipt.get("inputSha256") != digest(request):
            # An explicitly reviewed retry may use different sampling. Keep both the
            # actual generation request and the exact production request it replaces.
            if request.get("modelId") and receipt.get("acceptedForInputSha256") == digest(request):
                return "accepted"
            return "stale"
        if not request.get("modelId"):
            return "unverified"
        return "fresh"
    except (ValueError, OSError):
        return "unverified"


def record(audio: Path, line: dict, request: dict, *, endpoint: str, settings: dict | None = None) -> None:
    write(receipt_path(audio), {"schemaVersion": 1, "lineId": line["id"], "request": request,
          "inputSha256": digest(request), "outputSha256": sha256(audio), "endpoint": endpoint,
          "createdAt": datetime.now(timezone.utc).isoformat(), "provenance": "generated", "settings": settings})


def copy_receipt(source: Path, destination: Path, *, accepted_request: dict | None = None,
                 accepted_settings: dict | None = None) -> None:
    path = receipt_path(source)
    if path.exists():
        receipt = read(path)
        if receipt.get("outputSha256") != sha256(destination) or sha256(source) != sha256(destination):
            raise ValueError(f"Receipt does not match applied audio: {destination}")
        if accepted_request:
            receipt["acceptedForInputSha256"] = digest(accepted_request)
            receipt["acceptedForRequest"] = accepted_request
            receipt["acceptedSettings"] = accepted_settings
            receipt["acceptance"] = "reviewed-repair"
        write(receipt_path(destination), receipt)
    # A stale old receipt is intentionally not blessed when a legacy candidate has no receipt.
