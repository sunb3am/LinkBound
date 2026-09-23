import asyncio

import pytest

from app.access import TailscaleAuthMiddleware
from app.settings import load_settings


async def invoke(middleware, scope):
    events = []

    async def app(_scope, _receive, _send):
        events.append({"type": "app.called"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        events.append(event)

    wrapped = TailscaleAuthMiddleware(app, enabled=middleware[0], allowed_users=middleware[1])
    await wrapped(scope, receive, send)
    return events


def http_scope(path="/", host="127.0.0.1", login=None):
    headers = [(b"host", b"linkbound.local")]
    if login is not None:
        headers.append((b"tailscale-user-login", login.encode()))
    return {"type": "http", "method": "GET", "path": path, "headers": headers, "client": (host, 1234)}


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/api/config", "/api/v1/health", "/api/screenshot", "/docs", "/openapi.json"])
def test_denies_every_http_route_without_allowlisted_tailscale_identity(path):
    events = asyncio.run(invoke((True, ["owner@example.com"]), http_scope(path)))
    assert events[0]["status"] == 403
    assert not any(event["type"] == "app.called" for event in events)


def test_allows_allowlisted_identity_from_loopback_proxy():
    scope = http_scope("/api/config", login="Owner@Example.com")
    events = asyncio.run(invoke((True, ["owner@example.com"]), scope))
    assert events == [{"type": "app.called"}]


@pytest.mark.parametrize("host", ["100.64.0.1", "192.168.1.5", "unknown"])
def test_does_not_trust_identity_header_from_non_loopback_client(host):
    events = asyncio.run(invoke((True, ["owner@example.com"]), http_scope(host=host, login="owner@example.com")))
    assert events[0]["status"] == 403
    assert not any(event["type"] == "app.called" for event in events)


def test_empty_allowlist_denies_even_with_loopback_identity():
    events = asyncio.run(invoke((True, []), http_scope(login="owner@example.com")))
    assert events[0]["status"] == 403


def test_disabled_auth_preserves_local_app_access():
    events = asyncio.run(invoke((False, []), http_scope("/api/config")))
    assert events == [{"type": "app.called"}]


def test_websocket_requires_allowlisted_loopback_identity():
    scope = {"type": "websocket", "path": "/ws", "headers": [], "client": ("127.0.0.1", 1234)}
    events = asyncio.run(invoke((True, ["owner@example.com"]), scope))
    assert events == [{"type": "websocket.close", "code": 4403, "reason": "Forbidden"}]


def test_websocket_allows_allowlisted_loopback_identity():
    scope = {
        "type": "websocket", "path": "/ws",
        "headers": [(b"tailscale-user-login", b"owner@example.com")],
        "client": ("::1", 1234),
    }
    events = asyncio.run(invoke((True, ["owner@example.com"]), scope))
    assert events == [{"type": "app.called"}]


@pytest.mark.parametrize("value,users", [("treu", "owner@example.com"), ("true", "")])
def test_hosted_auth_misconfiguration_fails_at_startup(tmp_path, monkeypatch, value, users):
    monkeypatch.setenv("LINKBOUND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LINKBOUND_REQUIRE_TAILSCALE_AUTH", value)
    monkeypatch.setenv("LINKBOUND_TAILSCALE_ALLOWED_USERS", users)
    with pytest.raises(ValueError, match="LINKBOUND_"):
        load_settings()


def test_profile_root_keeps_sender_login_outside_code_release(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKBOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LINKBOUND_PROFILE_ROOT", str(tmp_path))
    monkeypatch.setenv("LINKBOUND_REQUIRE_TAILSCALE_AUTH", "false")
    settings = load_settings()
    assert settings.profile_path("me") == (tmp_path / "profiles" / "me").resolve()
    settings.operators["me"].profile_dir = "../escape"
    with pytest.raises(ValueError, match="profile_dir"):
        settings.profile_path("me")
