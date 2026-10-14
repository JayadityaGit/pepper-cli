"""``User-Agent`` on every request, and the 426 Upgrade Required exit path."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

import httpx
import pytest

from pepper_cli import _version, api_client, cli
from pepper_cli.secret import SecretStr

_WHOAMI = {"user_id": "u1", "email": "alice@example.com", "display_name": "Alice"}
_DEVICE_START = {
    "device_code": "pep_dev_abc",
    "user_code": "ABCD-EFGH",
    "verification_url": "https://example.com/activate",
    "expires_in": 600,
    "interval": 5,
}


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _api(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com", token=SecretStr("pep_pat_abc"), client=_mock(handler)
    )


def _recorder(response: httpx.Response, seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    return handler


# ---------------------------------------------------------------------------
# version / User-Agent


def test_user_agent_is_pepper_cli_slash_installed_version() -> None:
    installed = importlib.metadata.version("vycari-pepper")
    assert f"pepper-cli/{installed}" == api_client.USER_AGENT
    assert _version.get_version() == installed


def test_version_falls_back_when_metadata_is_missing(monkeypatch) -> None:
    def missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(_version, "version", missing)
    assert _version.get_version() == "0+unknown"


def test_normal_request_sends_user_agent() -> None:
    seen: list[httpx.Request] = []
    with _api(_recorder(httpx.Response(200, json=_WHOAMI), seen)) as api:
        api.whoami()
    assert seen[0].headers["user-agent"] == api_client.USER_AGENT
    assert re.fullmatch(r"pepper-cli/\S+", seen[0].headers["user-agent"])


def test_streaming_request_sends_user_agent() -> None:
    seen: list[httpx.Request] = []
    body = 'event: reply\ndata: {"reply": "hi"}\n\n'
    resp = httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    with _api(_recorder(resp, seen)) as api:
        list(api.chat_send_stream("hi"))
    assert seen[0].headers["user-agent"] == api_client.USER_AGENT


def test_feed_stream_and_takeout_send_user_agent(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    feed = httpx.Response(200, content=": ping\n\n", headers={"content-type": "text/event-stream"})
    with _api(_recorder(feed, seen)) as api:
        list(api.watch_agent_events())
    zipped = httpx.Response(200, content=b"zip")
    with _api(_recorder(zipped, seen)) as api:
        api.download_takeout(out_path=str(tmp_path / "t.zip"))
    assert [r.headers["user-agent"] for r in seen] == [api_client.USER_AGENT] * 2


def test_login_flow_sends_user_agent() -> None:
    seen: list[httpx.Request] = []
    api_client.device_start(
        "http://example.com",
        "alice@laptop",
        client=_mock(_recorder(httpx.Response(201, json=_DEVICE_START), seen)),
    )
    api_client.device_poll(
        "http://example.com",
        SecretStr("pep_dev_abc"),
        client=_mock(_recorder(httpx.Response(200, json={"status": "pending"}), seen)),
    )
    assert len(seen) == 2
    assert [r.headers["user-agent"] for r in seen] == [api_client.USER_AGENT] * 2


def test_owned_device_client_sends_user_agent(httpx_mock) -> None:
    httpx_mock.add_response(json=_DEVICE_START, status_code=201)
    api_client.device_start("http://example.com", "alice@laptop")
    assert httpx_mock.get_requests()[0].headers["user-agent"] == api_client.USER_AGENT


# ---------------------------------------------------------------------------
# 426 Upgrade Required


def test_426_json_detail_is_the_message() -> None:
    resp = httpx.Response(426, json={"detail": "Upgrade with: uv tool upgrade vycari-pepper"})
    with _api(lambda _: resp) as api, pytest.raises(api_client.UpgradeRequiredError) as ei:
        api.whoami()
    assert ei.value.message == "Upgrade with: uv tool upgrade vycari-pepper"


def test_426_plain_text_body_is_the_message() -> None:
    resp = httpx.Response(426, text="Please upgrade to 0.2.\nMore detail.\n")
    with _api(lambda _: resp) as api, pytest.raises(api_client.UpgradeRequiredError) as ei:
        api.whoami()
    assert ei.value.message == "Please upgrade to 0.2."


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(426),
        httpx.Response(426, text="  \n"),
        httpx.Response(426, json={"detail": ["not", "a string"]}),
        httpx.Response(426, json={}),
    ],
)
def test_426_without_usable_body_uses_the_default_message(resp: httpx.Response) -> None:
    with _api(lambda _: resp) as api, pytest.raises(api_client.UpgradeRequiredError) as ei:
        api.whoami()
    assert ei.value.message == api_client.DEFAULT_UPGRADE_MESSAGE
    assert "uv tool upgrade vycari-pepper" in ei.value.message


def test_426_on_streaming_paths(tmp_path: Path) -> None:
    resp = httpx.Response(426, json={"detail": "Upgrade now"})
    with _api(lambda _: resp) as api:
        with pytest.raises(api_client.UpgradeRequiredError, match="Upgrade now"):
            list(api.chat_send_stream("hi"))
        with pytest.raises(api_client.UpgradeRequiredError, match="Upgrade now"):
            list(api.watch_agent_events())
        with pytest.raises(api_client.UpgradeRequiredError, match="Upgrade now"):
            api.download_takeout(out_path=str(tmp_path / "t.zip"))
    assert not (tmp_path / "t.zip").exists()


def test_426_on_device_login_flow() -> None:
    resp = httpx.Response(426, json={"detail": "Upgrade now"})
    with pytest.raises(api_client.UpgradeRequiredError):
        api_client.device_start("http://example.com", "dev", client=_mock(lambda _: resp))
    with pytest.raises(api_client.UpgradeRequiredError):
        api_client.device_poll("http://example.com", SecretStr("x"), client=_mock(lambda _: resp))


def test_other_errors_are_still_api_errors() -> None:
    resp = httpx.Response(500, json={"detail": "boom"})
    with _api(lambda _: resp) as api, pytest.raises(api_client.ApiError) as ei:
        api.whoami()
    assert (ei.value.status, ei.value.detail) == (500, "boom")


def test_cli_prints_upgrade_message_to_stderr_and_exits_nonzero_without_retry(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    seen: list[httpx.Request] = []
    real_client = httpx.Client
    resp = httpx.Response(426, json={"detail": "Upgrade with: uv tool upgrade vycari-pepper"})

    def make_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(_recorder(resp, seen))
        return real_client(**kwargs)

    monkeypatch.setattr(api_client.httpx, "Client", make_client)
    rc = cli.main(["login", "--server", "http://example.com", "--token", "pep_pat_abc"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.strip() == "Upgrade with: uv tool upgrade vycari-pepper"
    assert "Traceback" not in captured.err
    assert "pep_pat_abc" not in captured.out + captured.err
    assert len(seen) == 1
