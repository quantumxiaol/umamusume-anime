from __future__ import annotations

import json

import httpx
import pytest

from my_tts import cli


def make_client(
    handler,
    *,
    base_url: str = "http://127.0.0.1:8002",
) -> cli.FishSpeechClient:
    http_client = httpx.Client(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
    )
    return cli.FishSpeechClient(
        base_url=base_url,
        timeout=300.0,
        client=http_client,
    )


def test_shutdown_sends_confirmation_header_and_waits_for_exit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fishspeech/admin/shutdown":
            return httpx.Response(
                202,
                json={"status": "accepted", "reason": "admin_request"},
            )
        raise httpx.ConnectError("server stopped", request=request)

    client = make_client(handler)
    try:
        payload = client.shutdown(wait=True, wait_timeout=1.0)
    finally:
        client.close()

    assert payload == {
        "status": "accepted",
        "reason": "admin_request",
        "server_stopped": True,
    }
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/fishspeech/admin/shutdown"
    assert requests[0].headers[cli.ADMIN_SHUTDOWN_HEADER] == cli.ADMIN_SHUTDOWN_VALUE
    assert requests[0].headers["Connection"] == "close"
    assert requests[1].url.path == "/fishspeech/health"


def test_shutdown_client_does_not_wait_unless_requested() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            202,
            json={"status": "accepted", "reason": "admin_request"},
        )

    client = make_client(handler)
    try:
        payload = client.shutdown()
    finally:
        client.close()

    assert payload == {"status": "accepted", "reason": "admin_request"}
    assert len(requests) == 1


@pytest.mark.parametrize(
    "health_response",
    [
        httpx.Response(503, text="listener is stopping"),
        httpx.Response(200, json={"status": "shutting_down"}),
    ],
)
def test_shutdown_wait_treats_nonhealthy_response_as_stopped(
    health_response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fishspeech/admin/shutdown":
            return httpx.Response(
                202,
                json={"status": "accepted", "reason": "admin_request"},
            )
        return health_response

    client = make_client(handler)
    try:
        payload = client.shutdown(wait=True, wait_timeout=1.0)
    finally:
        client.close()

    assert payload["server_stopped"] is True


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8002",
        "http://LOCALHOST.:8002/fishspeech",
        "http://127.0.0.1:8002",
        "http://127.42.0.1:8002",
        "http://[::1]:8002",
        "http://[::ffff:127.0.0.1]:8002",
    ],
)
def test_shutdown_accepts_only_explicit_loopback_urls(base_url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={"status": "accepted", "reason": "admin_request"},
        )

    client = make_client(handler, base_url=base_url)
    try:
        assert client.shutdown()["status"] == "accepted"
    finally:
        client.close()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://0.0.0.0:8002",
        "http://192.168.1.20:8002",
        "http://fish-speech.local:8002",
        "https://example.com",
    ],
)
def test_shutdown_rejects_non_loopback_url_before_request(base_url: str) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(202, json={"status": "accepted"})

    client = make_client(handler, base_url=base_url)
    try:
        with pytest.raises(cli.CliError, match="restricted to a loopback"):
            client.shutdown()
    finally:
        client.close()

    assert not called


def test_wait_until_stopped_times_out_while_health_remains_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    moments = iter([0.0, 0.0, 0.0, 1.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    client = make_client(handler)
    try:
        with pytest.raises(cli.CliError, match="still responds"):
            client.wait_until_stopped(timeout=0.5)
    finally:
        client.close()


def test_fish_shutdown_cli_waits_by_default_and_can_opt_out() -> None:
    parser = cli.build_parser()

    default_args = parser.parse_args(["fish", "shutdown"])
    assert default_args.base_url == cli.DEFAULT_FISH_TTS_URL
    assert default_args.wait_timeout == 30.0
    assert default_args.no_wait is False

    no_wait_args = parser.parse_args(
        ["fish", "shutdown", "--no-wait", "--wait-timeout", "12.5"]
    )
    assert no_wait_args.no_wait is True
    assert no_wait_args.wait_timeout == 12.5


def test_cmd_fish_shutdown_emits_confirmed_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[bool, float]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout: float) -> None:
            assert base_url == cli.DEFAULT_FISH_TTS_URL
            assert timeout == 300.0

        def shutdown(self, *, wait: bool, wait_timeout: float) -> dict[str, object]:
            calls.append((wait, wait_timeout))
            return {
                "status": "accepted",
                "reason": "admin_request",
                "server_stopped": True,
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "FishSpeechClient", FakeClient)
    args = cli.build_parser().parse_args(["fish", "shutdown"])

    assert cli.cmd_fish(args) == 0
    assert calls == [(True, 30.0)]
    assert json.loads(capsys.readouterr().out) == {
        "status": "accepted",
        "reason": "admin_request",
        "server_stopped": True,
    }
