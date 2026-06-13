"""TaskLane MCP ASGI auth middleware and the public /pair endpoint."""

from __future__ import annotations

import hmac
import json

from tasklane import pairing
from tasklane.mcp.core import (
    _audit, _MAX_CLIENT_NAME_LENGTH, _MAX_PAIR_BODY_BYTES, logger,
)
from tasklane.mcp.status import _status_data, _status_html


# --------------------------------------------------------------------------- #
# ASGI auth middleware + entrypoint
# --------------------------------------------------------------------------- #
async def _read_body(receive) -> bytes:
    """Drain an ASGI request body, stopping just past the /pair size cap."""
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > _MAX_PAIR_BODY_BYTES or not message.get("more_body"):
            return body


def _parse_pair_name(body: bytes) -> str | None:
    """Extract a valid client name from a /pair JSON body, or None if invalid."""
    if len(body) > _MAX_PAIR_BODY_BYTES:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or len(name) > _MAX_CLIENT_NAME_LENGTH:
        return None
    return name


class AuthMiddleware:
    """Pure-ASGI gate: app bearer token or approved paired-client token,
    plus Origin validation (anti DNS-rebind)."""

    def __init__(self, app, *, token: str, allowed_origins: set[str], pairing_enabled: bool = False):
        self.app = app
        self.token = token
        self.allowed_origins = allowed_origins
        self.pairing_enabled = pairing_enabled

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        if path == "/health":
            return await self._json(send, 200, {"status": "ok"})
        client_ip = headers.get("cf-connecting-ip") or (headers.get("x-forwarded-for", "").split(",")[0].strip()) or "?"
        if path == "/pair":
            return await self._pair(scope, receive, send, client_ip)
        if path == "/status":
            # browser-friendly: token via ?token= or Authorization header
            from urllib.parse import parse_qs
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            qtok = (qs.get("token") or [""])[0]
            auth = headers.get("authorization", "")
            htok = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            supplied = qtok or htok
            if not self.token or not hmac.compare_digest(supplied, self.token):
                _audit("status", {"ip": client_ip, "reason": "bad-token"}, "rejected")
                return await self._html(send, 401, "<h3>401 — append ?token=&lt;app_token&gt;</h3>")
            try:
                return await self._html(send, 200, _status_html(_status_data()))
            except Exception as exc:  # noqa: BLE001
                return await self._html(send, 500, f"<pre>status error: {exc}</pre>")
        origin = headers.get("origin")
        if origin and origin not in self.allowed_origins:
            _audit("auth", {"ip": client_ip, "path": path, "reason": "origin"}, "rejected")
            return await self._json(send, 403, {"error": "origin not allowed"})
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if not self._authorized(token):
            _audit("auth", {"ip": client_ip, "path": path, "reason": "bad-token"}, "rejected")
            return await self._json(send, 401, {"error": "invalid or missing bearer token"})
        return await self.app(scope, receive, send)

    def _authorized(self, token: str) -> bool:
        """True for the legacy app token or an approved, non-revoked paired client.

        Fails closed: any error in the pairing path denies (clean 401) rather
        than propagating out of the ASGI middleware as a 500.
        """
        if self.token and hmac.compare_digest(token, self.token):
            return True
        if not self.pairing_enabled or not token:
            return False
        try:
            return pairing.authenticate(token) is not None
        except Exception:  # noqa: BLE001 — auth must never 500
            logger.warning("pairing.authenticate raised; denying", exc_info=True)
            return False

    async def _pair(self, scope, receive, send, client_ip: str) -> None:
        """Public pairing endpoint: POST {"name": ...} -> pending client + one-time token."""
        if not self.pairing_enabled:
            _audit("pair", {"ip": client_ip, "reason": "disabled"}, "rejected")
            return await self._json(send, 404, {"error": "pairing is disabled"})
        if scope.get("method") != "POST":
            _audit("pair", {"ip": client_ip, "reason": "method"}, "rejected")
            return await self._json(send, 405, {"error": "use POST"})
        name = _parse_pair_name(await _read_body(receive))
        if name is None:
            _audit("pair", {"ip": client_ip, "reason": "bad-body"}, "rejected")
            return await self._json(send, 400, {"error": 'expected JSON body {"name": "<client name>"}'})
        try:
            result = pairing.request_pairing(name)
        except ValueError as exc:  # pending cap reached
            _audit("pair", {"ip": client_ip, "name": name, "reason": "pending-cap"}, "rejected")
            return await self._json(send, 429, {"error": str(exc)})
        _audit("pair", {"ip": client_ip, "name": name, "client_id": result["client_id"],
                        "pairing_code": result["pairing_code"]}, "ok")
        return await self._json(send, 200, result)

    @staticmethod
    async def _json(send, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _html(send, status: int, html: str) -> None:
        body = html.encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

