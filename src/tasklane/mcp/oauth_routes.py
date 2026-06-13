"""OAuth 2.1 HTTP endpoints for the MCP connector flow (pure ASGI).

Wires :mod:`tasklane.oauth` to the wire: discovery metadata, Dynamic Client
Registration, the authorize/consent page, and the token endpoint. The consent
step is the operator gate — the user enters the ``app_token`` once in the
browser to approve a connector; the client then holds its own rotating OAuth
token. Kept separate from :mod:`tasklane.mcp.auth` so the middleware stays small.
"""

from __future__ import annotations

import hmac
import html
import json
from urllib.parse import parse_qs, urlencode

from tasklane import oauth
from tasklane.mcp.core import _audit, logger

_MAX_BODY_BYTES = 16_384


# --------------------------------------------------------------------------- #
# small ASGI helpers
# --------------------------------------------------------------------------- #
async def _read_body(receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > _MAX_BODY_BYTES or not message.get("more_body"):
            return body


async def _json(send, status, payload, *, extra_headers=None) -> None:
    body = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json"),
               (b"cache-control", b"no-store"),
               (b"content-length", str(len(body)).encode())]
    headers += extra_headers or []
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _html(send, status, markup) -> None:
    body = markup.encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/html; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _redirect(send, location) -> None:
    await send({"type": "http.response.start", "status": 302,
                "headers": [(b"location", location.encode("latin-1")),
                            (b"cache-control", b"no-store"), (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


def _query(scope) -> dict[str, str]:
    raw = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    return {k: v[0] for k, v in raw.items()}


def issuer_from(headers: dict[str, str]) -> str:
    """Public base URL of this server, from the proxy/tunnel headers."""
    host = headers.get("host", "127.0.0.1")
    scheme = (headers.get("x-forwarded-proto", "") or "https").split(",")[0].strip() or "https"
    return f"{scheme}://{host}"


def resource_uri(issuer: str) -> str:
    """Canonical MCP resource URI (the /mcp endpoint), RFC 8707 / RFC 9728."""
    return f"{issuer}/mcp"


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #
class OAuthEndpoints:
    """Dispatch target for the OAuth paths. Holds the operator ``app_token``
    used as the consent secret."""

    PATHS = {
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/register", "/authorize", "/token",
    }

    def __init__(self, *, app_token: str, enabled: bool):
        self.app_token = app_token
        self.enabled = enabled

    async def handle(self, scope, receive, send, headers, client_ip) -> None:
        path = scope.get("path", "")
        if not self.enabled:
            return await _json(send, 404, {"error": "oauth is disabled"})
        issuer = issuer_from(headers)
        if path == "/.well-known/oauth-protected-resource":
            return await _json(send, 200, oauth.protected_resource_metadata(issuer, resource_uri(issuer)))
        if path == "/.well-known/oauth-authorization-server":
            return await _json(send, 200, oauth.authorization_server_metadata(issuer))
        if path == "/register":
            return await self._register(scope, receive, send, client_ip)
        if path == "/authorize":
            return await self._authorize(scope, receive, send, headers, client_ip, issuer)
        if path == "/token":
            return await self._token(scope, receive, send, client_ip)
        return await _json(send, 404, {"error": "not found"})

    async def _register(self, scope, receive, send, client_ip) -> None:
        if scope.get("method") != "POST":
            return await _json(send, 405, {"error": "use POST"})
        try:
            payload = json.loads((await _read_body(receive)).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
            record = oauth.register_client(
                redirect_uris=payload.get("redirect_uris") or [],
                client_name=payload.get("client_name"),
            )
        except ValueError as exc:
            _audit("oauth_register", {"ip": client_ip, "reason": str(exc)}, "rejected")
            return await _json(send, 400, {"error": "invalid_client_metadata", "error_description": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("oauth register failed: %s", exc)
            return await _json(send, 400, {"error": "invalid_client_metadata"})
        _audit("oauth_register", {"ip": client_ip, "client_id": record["client_id"],
                                  "client_name": record["client_name"]}, "ok")
        # RFC 7591: 201 with the client metadata (no secret — public client)
        return await _json(send, 201, record)

    async def _authorize(self, scope, receive, send, headers, client_ip, issuer) -> None:
        method = scope.get("method")
        params = _query(scope) if method == "GET" else _parse_form(await _read_body(receive))
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        # Validate client + redirect BEFORE trusting redirect_uri — never redirect
        # to an unregistered URI (open-redirect / attacker control).
        try:
            oauth.validate_authorization_request(client_id, redirect_uri)
        except ValueError as exc:
            _audit("oauth_authorize", {"ip": client_ip, "reason": str(exc)}, "rejected")
            return await _html(send, 400, _error_page(f"Invalid authorization request: {html.escape(str(exc))}"))

        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")
        resource = params.get("resource", resource_uri(issuer))
        scope_param = params.get("scope", "tasklane")

        # OAuth 2.1: public clients MUST use PKCE S256.
        if params.get("response_type", "code") != "code":
            return await _redirect(send, _err_redirect(redirect_uri, "unsupported_response_type", state))
        if not code_challenge or code_challenge_method != "S256":
            return await _redirect(send, _err_redirect(redirect_uri, "invalid_request", state, "PKCE S256 required"))

        if method == "GET":
            return await _html(send, 200, _consent_page(params, issuer))

        # POST = consent submission. Verify the operator's app_token.
        supplied = params.get("app_token", "")
        if not self.app_token or not hmac.compare_digest(supplied, self.app_token):
            _audit("oauth_authorize", {"ip": client_ip, "client_id": client_id, "reason": "bad-app-token"}, "rejected")
            return await _html(send, 401, _consent_page(params, issuer, error="Incorrect app token — try again."))
        try:
            code = oauth.issue_code(
                client_id=client_id, redirect_uri=redirect_uri,
                code_challenge=code_challenge, code_challenge_method=code_challenge_method,
                resource=resource, scope=scope_param, state=state,
            )
        except ValueError as exc:
            return await _redirect(send, _err_redirect(redirect_uri, "server_error", state, str(exc)))
        _audit("oauth_authorize", {"ip": client_ip, "client_id": client_id}, "approved")
        query = {"code": code}
        if state:
            query["state"] = state
        return await _redirect(send, f"{redirect_uri}?{urlencode(query)}")

    async def _token(self, scope, receive, send, client_ip) -> None:
        if scope.get("method") != "POST":
            return await _json(send, 405, {"error": "invalid_request"})
        form = _parse_form(await _read_body(receive))
        grant = form.get("grant_type", "")
        try:
            if grant == "authorization_code":
                tokens = oauth.exchange_code(
                    code=form.get("code", ""), code_verifier=form.get("code_verifier", ""),
                    client_id=form.get("client_id", ""), redirect_uri=form.get("redirect_uri", ""),
                )
            elif grant == "refresh_token":
                tokens = oauth.refresh_tokens(
                    refresh_token=form.get("refresh_token", ""), client_id=form.get("client_id", ""),
                )
            else:
                return await _json(send, 400, {"error": "unsupported_grant_type"})
        except ValueError as exc:
            _audit("oauth_token", {"ip": client_ip, "grant": grant, "reason": str(exc)}, "rejected")
            return await _json(send, 400, {"error": "invalid_grant", "error_description": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("oauth token failed: %s", exc)
            return await _json(send, 400, {"error": "invalid_request"})
        _audit("oauth_token", {"ip": client_ip, "grant": grant}, "ok")
        return await _json(send, 200, tokens)


def _parse_form(body: bytes) -> dict[str, str]:
    raw = parse_qs(body.decode("utf-8", errors="replace"))
    return {k: v[0] for k, v in raw.items()}


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def _hidden(params: dict[str, str], keys) -> str:
    return "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(params.get(k, ""))}">'
        for k in keys
    )


def _consent_page(params: dict[str, str], issuer: str, *, error: str = "") -> str:
    client = oauth.get_client(params.get("client_id", "")) or {}
    name = html.escape(str(client.get("client_name") or "An MCP client"))
    redirect = html.escape(params.get("redirect_uri", ""))
    err = f'<p style="color:#c62828">{html.escape(error)}</p>' if error else ""
    hidden = _hidden(params, ["client_id", "redirect_uri", "state", "code_challenge",
                              "code_challenge_method", "resource", "scope", "response_type"])
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Authorize TaskLane connector</title>
<style>body{{font-family:system-ui,Arial;margin:0;background:#f4f1ea;color:#222}}
.card{{max-width:420px;margin:8vh auto;background:#fff;padding:28px;border-radius:12px;box-shadow:0 2px 20px #0001}}
h2{{margin:0 0 4px}}p{{color:#555;line-height:1.5}}input[type=password]{{width:100%;padding:10px;font-size:16px;
border:1px solid #ccc;border-radius:8px;box-sizing:border-box}}button{{margin-top:14px;width:100%;padding:12px;
font-size:16px;background:#1565c0;color:#fff;border:0;border-radius:8px;cursor:pointer}}code{{font-size:13px}}</style>
</head><body><div class=card>
<h2>Connect to TaskLane</h2>
<p><b>{name}</b> wants to connect to your TaskLane control plane.<br>
Redirect: <code>{redirect}</code></p>
{err}
<form method=POST action="/authorize">
{hidden}
<label>Enter your TaskLane app token to approve:</label>
<input type=password name=app_token autocomplete=off autofocus placeholder="app_token">
<button type=submit>Approve connection</button>
</form>
<p style="font-size:12px;color:#888;margin-top:16px">Only approve clients you initiated. The token grants this client
ongoing access until you revoke it.</p>
</div></body></html>"""


def _error_page(message: str) -> str:
    return (f"<!doctype html><html><head><meta charset=utf-8><title>Authorization error</title></head>"
            f"<body style='font-family:system-ui;margin:8vh auto;max-width:420px'>"
            f"<h3>Authorization error</h3><p>{message}</p></body></html>")


def _err_redirect(redirect_uri: str, error: str, state: str, description: str = "") -> str:
    query = {"error": error}
    if description:
        query["error_description"] = description
    if state:
        query["state"] = state
    return f"{redirect_uri}?{urlencode(query)}"
