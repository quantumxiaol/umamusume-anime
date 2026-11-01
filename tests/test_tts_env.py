import json

import httpx
import pytest

from my_tts import cli, config
from scripts import synthesize_script, video_workflow


def set_env_file(text):
    config.ENV_FILE.write_text(text)


def test_dotenv_process_and_cli_precedence(monkeypatch):
    set_env_file('TTS_PROVIDER=fishspeech\nTTS_API_BASE_URL="http://gpu:18002/fishspeech"\n'
                 'TTS_MODEL=s2-pro\nTTS_LANGUAGE=ja\nTTS_API_KEY="secret # value"\n')
    parser = synthesize_script.build_parser()
    args = parser.parse_args(["--script", "example.json"])
    assert (args.tts_engine, args.fish_tts_url, args.model_id, args.language) == (
        "fishspeech", "http://gpu:18002/fishspeech", "s2-pro", "ja")
    assert config.get_tts_settings().api_key("fishspeech") == "secret # value"
    assert "secret" not in repr(config.get_tts_settings())
    monkeypatch.setenv("TTS_API_BASE_URL", "http://process:8002")
    assert cli.build_parser().parse_args(["fish", "health"]).base_url == "http://process:8002"
    args = parser.parse_args(["--script", "example.json", "--fish-tts-url", "http://explicit:8002",
                              "--model-id", "custom-v2"])
    assert (args.fish_tts_url, args.model_id) == ("http://explicit:8002", "custom-v2")
    monkeypatch.setenv("TTS_API_KEY", "")
    assert config.get_tts_settings().api_key("fishspeech") == ""


def test_backend_override_does_not_reuse_fish_settings():
    set_env_file("TTS_PROVIDER=fishspeech\nTTS_API_BASE_URL=http://gpu:18002\nTTS_MODEL=s2-pro\nTTS_API_KEY=secret\n")
    args = synthesize_script.build_parser().parse_args(["--script", "x", "--tts-engine", "qwen3tts"])
    assert args.qwen3tts_url == cli.DEFAULT_QWEN3TTS_URL
    assert args.model_id is None
    assert config.get_tts_settings().api_key("qwen3tts") == ""


def test_qwen_language_alias_and_fish_key_fallback(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "qwen3tts")
    monkeypatch.setenv("TTS_LANGUAGE", "ja")
    monkeypatch.setenv("FISH_TTS_API_KEY", "fish-only")
    assert config.get_tts_settings().language("qwen3tts") == "Japanese"
    assert config.get_tts_settings().api_key("fishspeech") == "fish-only"
    assert config.get_tts_settings().api_key("qwen3tts") == ""


def test_workflow_snapshots_defaults_without_secret(tmp_path):
    set_env_file("TTS_PROVIDER=fishspeech\nTTS_API_BASE_URL=http://gpu:18002\nTTS_MODEL=s2-pro\nTTS_API_KEY=secret\n")
    script, output = tmp_path / "script.json", tmp_path / "workflow.json"
    script.write_text(json.dumps({"projectId": "Smoke", "lines": [{"id": "a", "speakerId": "hero"}]}))
    assert video_workflow.main(["init", "--script", str(script), "--output", str(output)]) == 0
    tts = json.loads(output.read_text())["tts"]
    assert (tts["engine"], tts["url"], tts["modelId"]) == ("fishspeech", "http://gpu:18002", "s2-pro")
    assert "secret" not in output.read_text()


@pytest.mark.parametrize("client_class,provider,prefix", [
    (cli.FishSpeechClient, "fishspeech", "fishspeech"),
    (cli.Qwen3TTSClient, "qwen3tts", "qwen3tts"),
])
def test_auth_on_health_single_batch_and_download(monkeypatch, tmp_path, client_class, provider, prefix):
    monkeypatch.setenv("TTS_PROVIDER", provider)
    monkeypatch.setenv("TTS_API_KEY", "test-key")
    seen = []

    def handle(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        seen.append(request.url.path)
        if request.method == "POST":
            body = request.read()
            assert b'ref_audio' in body and 'こんにちは'.encode() in body
            assert b'name="model"' not in body
            if provider == "fishspeech":
                assert b'name="language"' not in body
        return httpx.Response(200, json={"status": "ok"})

    base = "http://gpu:18002"
    transport = httpx.Client(base_url=base, transport=httpx.MockTransport(handle))
    client = client_class(base + "/" + prefix, 10, client=transport)
    reference = tmp_path / "ref.wav"
    reference.write_bytes(b"reference")
    try:
        client.health()
        client.voice_clone(ref_audio_path=reference, text="こんにちは", ref_text="reference")
        client.voice_clone_batch_file(ref_audio_path=reference, texts=["こんにちは", "おはよう"], ref_text="reference")
        client.download(f"/{prefix}/files/a.wav", tmp_path / "out.wav")
        with pytest.raises(cli.CliError, match="different service origin"):
            client.download("http://unrelated.example/audio.wav", tmp_path / "bad.wav")
    finally:
        client.close()
    assert seen == [f"/{prefix}/health", f"/{prefix}/tts/voice_clone",
                    f"/{prefix}/tts/voice_clone_batch_file", f"/{prefix}/files/a.wav"]


def test_disabled_generation_stops_before_upload_but_health_works(monkeypatch, tmp_path):
    monkeypatch.setenv("TTS_ENABLED", "false")
    calls = []
    transport = httpx.Client(base_url="http://gpu", transport=httpx.MockTransport(
        lambda request: calls.append(request.method) or httpx.Response(200, json={"status": "ok"})))
    client = cli.FishSpeechClient("http://gpu", 10, client=transport)
    try:
        assert client.health()["status"] == "ok"
        with pytest.raises(ValueError, match="TTS_ENABLED=false"):
            client.voice_clone(ref_audio_path=tmp_path / "missing.wav", text="test")
        with pytest.raises(ValueError, match="TTS_ENABLED=false"):
            client.voice_clone_batch_file(ref_audio_path=tmp_path / "missing.wav", texts=["test"])
    finally:
        client.close()
    assert calls == ["GET"]
