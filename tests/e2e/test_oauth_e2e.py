"""E2E: the OAuth 2.1 connector flow over real HTTP — discovery metadata, the
401 WWW-Authenticate challenge, Dynamic Client Registration, the authorize/
consent step (wrong + right app_token), token exchange, using the OAuth access
token to drive a real MCP session, refresh rotation, and the disabled switch."""

import asyncio
import base64
import hashlib
import os
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import yaml

pytestmark = pytest.mark.e2e

TOKEN = "e2e-app-token"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(home_cfg: dict):
    import uvicorn
    from tasklane.config import load_config
    from tasklane.mcp_server import build_app

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(load_config()), host="127.0.0.1",
                                           port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn failed to start"
        time.sleep(0.05)
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def oauth_http(tmp_path_factory):
    home = tmp_path_factory.mktemp("oauth-home")
    (home / "config.yaml").write_text(yaml.safe_dump({
        "app_token": TOKEN, "mcp_host": "127.0.0.1", "oauth_enabled": True,
    }), encoding="utf-8")
    old = os.environ.get("TASKLANE_HOME")
    os.environ["TASKLANE_HOME"] = str(home)
    server, thread, url = _start({})
    yield url
    server.should_exit = True
    thread.join(timeout=10)
    os.environ.pop("TASKLANE_HOME", None) if old is None else os.environ.__setitem__("TASKLANE_HOME", old)


def _pkce():
    v = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    return v, c


def _register(url) -> str:
    r = httpx.post(f"{url}/register", json={"redirect_uris": [REDIRECT], "client_name": "claude.ai"})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


# --------------------------------------------------------------------------- #
# discovery + challenge
# --------------------------------------------------------------------------- #
def test_protected_resource_metadata(oauth_http):
    r = httpx.get(f"{oauth_http}/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    assert body["resource"].endswith("/mcp")
    assert body["authorization_servers"] and body["authorization_servers"][0].startswith("http")


def test_authorization_server_metadata(oauth_http):
    r = httpx.get(f"{oauth_http}/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    m = r.json()
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert m[key].startswith("http")
    assert m["code_challenge_methods_supported"] == ["S256"]


def test_unauthenticated_mcp_returns_www_authenticate(oauth_http):
    r = httpx.post(f"{oauth_http}/mcp", json={})
    assert r.status_code == 401
    www = r.headers.get("www-authenticate", "")
    assert "resource_metadata=" in www and "oauth-protected-resource" in www


# --------------------------------------------------------------------------- #
# DCR validation
# --------------------------------------------------------------------------- #
def test_register_rejects_bad_redirect(oauth_http):
    r = httpx.post(f"{oauth_http}/register", json={"redirect_uris": ["http://evil.example/cb"]})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"


def test_register_requires_redirect(oauth_http):
    assert httpx.post(f"{oauth_http}/register", json={}).status_code == 400


# --------------------------------------------------------------------------- #
# authorize + token (the full connector handshake)
# --------------------------------------------------------------------------- #
def test_authorize_get_renders_consent(oauth_http):
    cid = _register(oauth_http)
    _, challenge = _pkce()
    r = httpx.get(f"{oauth_http}/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
    })
    assert r.status_code == 200 and "app token" in r.text.lower()


def test_authorize_unknown_client_does_not_redirect(oauth_http):
    _, challenge = _pkce()
    r = httpx.get(f"{oauth_http}/authorize", params={
        "response_type": "code", "client_id": "tlc_nope", "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    assert r.status_code == 400  # never redirects to an unvalidated URI


def test_authorize_requires_pkce_s256(oauth_http):
    cid = _register(oauth_http)
    r = httpx.get(f"{oauth_http}/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "state": "s",
    }, follow_redirects=False)
    assert r.status_code == 302
    err = parse_qs(urlparse(str(r.headers["location"])).query)
    assert err["error"] == ["invalid_request"]


def test_authorize_wrong_app_token_denied(oauth_http):
    cid = _register(oauth_http)
    _, challenge = _pkce()
    r = httpx.post(f"{oauth_http}/authorize", data={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
        "app_token": "WRONG",
    }, follow_redirects=False)
    assert r.status_code == 401 and "Incorrect app token" in r.text


def test_full_connector_handshake_and_mcp_call(oauth_http):
    cid = _register(oauth_http)
    verifier, challenge = _pkce()
    # consent → code (302 to the redirect_uri with ?code=)
    r = httpx.post(f"{oauth_http}/authorize", data={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st8",
        "app_token": TOKEN,
    }, follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(str(r.headers["location"])).query)
    assert q["state"] == ["st8"]
    code = q["code"][0]

    # token exchange
    tok = httpx.post(f"{oauth_http}/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": cid, "redirect_uri": REDIRECT,
    })
    assert tok.status_code == 200, tok.text
    access = tok.json()["access_token"]
    refresh = tok.json()["refresh_token"]
    assert tok.json()["token_type"] == "Bearer"

    # the OAuth access token authenticates a real MCP session
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def call() -> set[str]:
        async with streamablehttp_client(f"{oauth_http}/mcp",
                                         headers={"Authorization": f"Bearer {access}"}) as (rd, wr, _):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                return {t.name for t in (await s.list_tools()).tools}

    assert "create_task" in asyncio.run(call())

    # refresh rotates and the new access token also works
    r2 = httpx.post(f"{oauth_http}/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh, "client_id": cid,
    })
    assert r2.status_code == 200 and r2.json()["access_token"] != access

    # reusing the old refresh token is denied
    r3 = httpx.post(f"{oauth_http}/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh, "client_id": cid,
    })
    assert r3.status_code == 400 and r3.json()["error"] == "invalid_grant"


def test_token_bad_code_is_invalid_grant(oauth_http):
    cid = _register(oauth_http)
    r = httpx.post(f"{oauth_http}/token", data={
        "grant_type": "authorization_code", "code": "bogus", "code_verifier": "x",
        "client_id": cid, "redirect_uri": REDIRECT,
    })
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_token_unsupported_grant(oauth_http):
    r = httpx.post(f"{oauth_http}/token", data={"grant_type": "password"})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"


# --------------------------------------------------------------------------- #
# disabled switch
# --------------------------------------------------------------------------- #
def test_oauth_disabled(tmp_path_factory):
    home = tmp_path_factory.mktemp("oauth-off")
    (home / "config.yaml").write_text(yaml.safe_dump({
        "app_token": TOKEN, "mcp_host": "127.0.0.1", "oauth_enabled": False,
    }), encoding="utf-8")
    old = os.environ.get("TASKLANE_HOME")
    os.environ["TASKLANE_HOME"] = str(home)
    server, thread, url = _start({})
    try:
        assert httpx.get(f"{url}/.well-known/oauth-protected-resource").status_code == 404
        # 401 on /mcp must NOT advertise oauth metadata when disabled
        r = httpx.post(f"{url}/mcp", json={})
        assert r.status_code == 401 and "www-authenticate" not in {k.lower() for k in r.headers}
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        os.environ.pop("TASKLANE_HOME", None) if old is None else os.environ.__setitem__("TASKLANE_HOME", old)
