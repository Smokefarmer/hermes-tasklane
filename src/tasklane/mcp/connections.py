"""Browser-facing /connections page: view and manage GitHub + Linear integrations.

This is the visible layer over :mod:`tasklane.integrations` — what's connected,
and a form to paste a token from a browser (the same thing the
``set_integration_token`` MCP tool does for a connected Claude). Gated by the
app_token, exactly like /status. Tokens are write-only here too: the page only
ever shows a masked hint.
"""

from __future__ import annotations

import hmac
import html
from urllib.parse import parse_qs

from tasklane import integrations
from tasklane.mcp.core import _audit
from tasklane.mcp.oauth_routes import _html, _parse_form, _read_body, _redirect


class ConnectionsPage:
    PATH = "/connections"

    def __init__(self, *, app_token: str):
        self.app_token = app_token

    async def handle(self, scope, receive, send, client_ip) -> None:
        method = scope.get("method")
        if method == "POST":
            form = _parse_form(await _read_body(receive))
            if not self._ok(form.get("app_token", "")):
                _audit("connections", {"ip": client_ip, "reason": "bad-token"}, "rejected")
                return await _html(send, 401, _page(error="Incorrect app token."))
            return await self._apply(send, form, client_ip)
        # GET — token via ?token=
        qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        token = (qs.get("token") or [""])[0]
        if not self._ok(token):
            return await _html(send, 401, _page(error="Append ?token=<app_token>", token=""))
        return await _html(send, 200, _page(token=token))

    def _ok(self, supplied: str) -> bool:
        return bool(self.app_token) and hmac.compare_digest(supplied, self.app_token)

    async def _apply(self, send, form, client_ip) -> None:
        action = form.get("action", "set")
        service = form.get("service", "")
        try:
            if action == "clear":
                integrations.clear_token(service)
                _audit("connections", {"ip": client_ip, "service": service, "action": "clear"}, "ok")
            else:
                integrations.set_token(service, form.get("token", ""),
                                       default_repo=form.get("default_repo") or None)
                _audit("connections", {"ip": client_ip, "service": service, "action": "set"}, "ok")
        except ValueError as exc:
            return await _html(send, 400, _page(error=str(exc), token=form.get("app_token", "")))
        # redirect back so a refresh doesn't resubmit (token stays in the URL, app-gated)
        return await _redirect(send, f"/connections?token={form.get('app_token', '')}")


def _row(service: str, st: dict, token: str) -> str:
    connected = st.get("connected")
    badge = ('<span style="color:#2e7d32;font-weight:600">connected</span>'
             if connected else '<span style="color:#999">not connected</span>')
    hint = f' — <code>{html.escape(str(st.get("token_hint", "")))}</code>' if connected else ""
    extra = ""
    if service == "github":
        repo = html.escape(str(st.get("default_repo") or ""))
        extra = (f'<input name=default_repo placeholder="default repo (owner/name)" '
                 f'value="{repo}" style="width:100%;padding:8px;margin-top:6px;'
                 f'border:1px solid #ccc;border-radius:6px;box-sizing:border-box">')
    disconnect = ""
    if connected:
        disconnect = (f'<form method=POST style="display:inline">'
                      f'<input type=hidden name=app_token value="{html.escape(token)}">'
                      f'<input type=hidden name=service value="{service}">'
                      f'<input type=hidden name=action value="clear">'
                      f'<button style="background:#c62828;color:#fff;border:0;border-radius:6px;'
                      f'padding:6px 10px;cursor:pointer;margin-top:6px">Disconnect</button></form>')
    return f"""<div style="background:#fff;padding:18px;border-radius:10px;margin:12px 0;box-shadow:0 1px 8px #0001">
<h3 style="margin:0 0 6px;text-transform:capitalize">{service} {badge}{hint}</h3>
<form method=POST>
<input type=hidden name=app_token value="{html.escape(token)}">
<input type=hidden name=service value="{service}">
<input name=token type=password autocomplete=off placeholder="paste {service} API token to connect/replace"
 style="width:100%;padding:8px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box">
{extra}
<button style="background:#1565c0;color:#fff;border:0;border-radius:6px;padding:8px 12px;cursor:pointer;margin-top:8px">
Save {service} token</button> {disconnect}
</form></div>"""


def _page(*, token: str = "", error: str = "") -> str:
    try:
        st = integrations.status()
    except Exception:  # noqa: BLE001
        st = {"github": {"connected": False}, "linear": {"connected": False}}
    err = f'<p style="color:#c62828">{html.escape(error)}</p>' if error else ""
    rows = _row("github", st.get("github", {}), token) + _row("linear", st.get("linear", {}), token)
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>TaskLane connections</title>
<style>body{{font-family:system-ui,Arial;margin:0;background:#f4f1ea;color:#222}}
.wrap{{max-width:520px;margin:6vh auto;padding:0 16px}}code{{font-size:13px}}h2{{margin-bottom:4px}}
p.help{{color:#666;line-height:1.5}}</style></head><body><div class=wrap>
<h2>Connections</h2>
<p class=help>Connect GitHub and Linear so a Claude client (or the bridges) can open issues and turn
Linear tickets into GitHub issues and TaskLane jobs. Tokens are stored mode-600 and never shown again.</p>
{err}
{rows}
</div></body></html>"""
