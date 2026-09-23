"""Private app access checks for requests arriving through Tailscale Serve."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class TailscaleAuthMiddleware:
    """Trust Tailscale's identity header only from a local proxy connection."""

    def __init__(self, app, *, enabled: bool, allowed_users: Iterable[str]):
        self.app = app
        self.enabled = enabled
        self.allowed_users = {user.strip().casefold() for user in allowed_users if user.strip()}

    async def __call__(self, scope, receive, send):
        if not self.enabled:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        client_host = client[0] if client else None
        headers = dict(scope.get("headers", []))
        login = headers.get(b"tailscale-user-login", b"").decode("latin-1").strip().casefold()
        allowed = _is_loopback(client_host) and bool(login) and login in self.allowed_users
        if allowed:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403, "reason": "Forbidden"})
            return
        if scope["type"] == "http":
            body = b"Forbidden"
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)
