from pathlib import Path

import pytest

from scripts import synthesize_script as synth
from scripts.tts_manifest import cache_status, copy_receipt, receipt_path, record, request_fingerprint
from scripts.workflow_io import read, write


@pytest.fixture
def voice(tmp_path):
    character = tmp_path / "characters/hero"
    character.mkdir(parents=True)
    (character / "reference.wav").write_bytes(b"curated reference")
    (character / "reference_jp.txt").write_text("reference text\n")
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"original audio")
    line = {"id": "a", "speakerId": "hero", "spokenText": "[soft tone]こんにちは", "subtitleZh": "你好", "audio": str(audio)}
    script = tmp_path / "script.json"
    write(script, {"lines": [line]})
    args = synth.build_parser().parse_args(["--script", str(script), "--characters-root", str(tmp_path / "characters"),
        "--tts-engine", "fishspeech", "--model-id", "s2-pro-curated-v1"])
    return line, args, audio, character


def test_legacy_is_unverified_and_does_not_contact_backend(voice, monkeypatch):
    line, args, audio, _ = voice
    assert cache_status(audio, request_fingerprint(line, args)) == "unverified"
    monkeypatch.setattr(synth, "FishSpeechClient", lambda **kw: pytest.fail("Must not contact TTS"))
    with pytest.raises(synth.SynthesisError, match="unverified"):
        synth.synthesize_script(args)
    args.reuse_unverified = True
    synth.synthesize_script(args)
    assert not receipt_path(audio).exists()


def test_subtitle_and_id_changes_do_not_invalidate_audio(voice):
    line, args, audio, _ = voice
    request = request_fingerprint(line, args)
    record(audio, line, request, endpoint="local")
    line.update(subtitleZh="中文字幕改了", subtitleJa="字幕", id="stable_new_name")
    assert cache_status(audio, request_fingerprint(line, args)) == "fresh"


@pytest.mark.parametrize("change", ["text", "speaker", "reference", "reftext", "model", "parameter", "wav"])
def test_each_voice_input_invalidates(voice, change):
    line, args, audio, character = voice
    record(audio, line, request_fingerprint(line, args), endpoint="local")
    if change == "text": line["spokenText"] += "！"
    if change == "reference": (character / "reference.wav").write_bytes(b"new reference")
    if change == "reftext": (character / "reference_jp.txt").write_text("different transcript")
    if change == "model": args.model_id = "s2-v2"
    if change == "parameter": args.temperature = 0.42
    if change == "speaker":
        import shutil
        shutil.copytree(character, character.parent / "other")
        line["speakerId"] = "other"
    if change == "wav": audio.write_bytes(b"externally replaced")
    assert cache_status(audio, request_fingerprint(line, args)) == ("modified" if change == "wav" else "stale")


def test_stale_audio_cannot_use_legacy_override(voice):
    line, args, audio, _ = voice
    record(audio, line, request_fingerprint(line, args), endpoint="local")
    args.temperature = 0.2
    args.reuse_unverified = True
    with pytest.raises(synth.SynthesisError, match="stale"):
        synth.synthesize_script(args)


def test_audit_is_non_mutating(voice, monkeypatch):
    monkeypatch.setenv("TTS_ENABLED", "false")
    _, args, audio, _ = voice
    args.audit = True
    args.audit_output = str(audio.parent / "audit.json")
    monkeypatch.setattr(synth, "FishSpeechClient", lambda **kw: pytest.fail("Must not contact TTS"))
    synth.synthesize_script(args)
    assert read(Path(args.audit_output))["lines"][0]["status"] == "unverified"
    assert not receipt_path(audio).exists()


def test_disabled_generation_does_not_contact_backend(voice, monkeypatch):
    _, args, audio, _ = voice
    audio.unlink()
    monkeypatch.setenv("TTS_ENABLED", "false")
    monkeypatch.setattr(synth, "FishSpeechClient", lambda **kw: pytest.fail("Must not contact TTS"))
    with pytest.raises(ValueError, match="TTS_ENABLED=false"):
        synth.synthesize_script(args)
    assert not audio.exists()


def test_unhealthy_service_does_not_generate_audio(voice, monkeypatch):
    _, args, audio, _ = voice
    audio.unlink()
    class UnhealthyClient:
        def __init__(self, **kw): pass
        def health(self): return {"status": "shutting_down"}
        def close(self): pass
    monkeypatch.setattr(synth, "FishSpeechClient", UnhealthyClient)
    with pytest.raises(synth.SynthesisError, match="not healthy"):
        synth.synthesize_script(args)
    assert not audio.exists()


def test_copy_receipt_binds_to_candidate_bytes(voice):
    line, args, audio, _ = voice
    record(audio, line, request_fingerprint(line, args), endpoint="local")
    target = audio.parent / "target.wav"
    target.write_bytes(b"different")
    with pytest.raises(ValueError): copy_receipt(audio, target)
    target.write_bytes(audio.read_bytes())
    copy_receipt(audio, target)
    assert cache_status(target, request_fingerprint(line, args)) == "fresh"


def test_unknown_model_is_not_claimed_verified(voice):
    line, args, audio, _ = voice
    args.model_id = None
    record(audio, line, request_fingerprint(line, args), endpoint="local")
    assert cache_status(audio, request_fingerprint(line, args)) == "unverified"


def test_accepted_retry_retains_actual_settings_and_invalidates_on_further_edits(voice):
    line, args, audio, _ = voice
    production = request_fingerprint(line, args)
    args.temperature = 0.25
    retry = request_fingerprint(line, args)
    record(audio, line, retry, endpoint="local")
    candidate = audio.parent / "candidate.wav"
    candidate.write_bytes(audio.read_bytes())
    copy_receipt(audio, candidate, accepted_request=production)
    assert cache_status(candidate, production) == "accepted"
    assert read(receipt_path(candidate))["request"]["parameters"]["temperature"] == 0.25
    changed = dict(production, text="different sentence")
    assert cache_status(candidate, changed) == "stale"


@pytest.mark.parametrize("batch", [True, False])
def test_generation_records_each_wav_then_reuses_without_service(voice, monkeypatch, batch):
    line, args, audio, _ = voice
    audio.unlink()
    args.no_batch = not batch
    class FakeClient:
        def __init__(self, **kw): pass
        def health(self): return {"status": "ok"}
        def voice_clone(self, **kw):
            assert kw["text"].startswith("<|speaker:0|>")
            return {"audio": {"filename": "voice.wav"}}
        def voice_clone_batch_file(self, **kw):
            assert kw["text_file"].read_text().startswith("<|speaker:0|>")
            return {"audio_paths": [{"filename": "voice.wav"}]}
        def close(self): pass
    def materialize(**kw):
        kw["destination"].write_bytes(b"generated")
    monkeypatch.setattr(synth, "FishSpeechClient", FakeClient)
    monkeypatch.setattr(synth, "materialize_fish_stored_audio", materialize)
    synth.synthesize_script(args)
    assert cache_status(audio, request_fingerprint(line, args)) == "fresh"
    monkeypatch.setattr(synth, "FishSpeechClient", lambda **kw: pytest.fail("Cached output must not contact TTS"))
    synth.synthesize_script(args)


def test_insert_does_not_invalidate_other_line_audio(voice):
    line, args, audio, _ = voice
    record(audio, line, request_fingerprint(line, args), endpoint="local")
    script = read(Path(args.script))
    script["lines"].insert(0, {"id": "inserted_stable_id", "subtitleZh": "新句"})
    write(Path(args.script), script)
    assert cache_status(audio, request_fingerprint(script["lines"][1], args)) == "fresh"
