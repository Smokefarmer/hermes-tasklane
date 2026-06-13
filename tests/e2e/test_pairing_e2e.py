"""E2E: client pairing over real HTTP — the public /pair endpoint, paired-token
auth on /mcp (approve/revoke lifecycle), the pairing admin MCP tools, and the
pairing_enabled kill switch."""

import asyncio
import os
import socket
import threading
import time

import httpx
import pytest
import yaml

pytestmark = pytest.mark.e2e

TOKEN = "e2e-test-token"

PAIRING_TOOLS = {"pairing_requests", "approve_client", "reject_client", "list_clients", "revoke_client"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server() -> tuple:
    """Start the real MCP app under uvicorn against the current $TASKLANE_HOME."""
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
def pairing_http(tmp_path_factory):
    """Server with pairing enabled (the default)."""
    home = tmp_path_factory.mktemp("tasklane-home")
    (home / "config.yaml").write_text(yaml.safe_dump({
        "app_token": TOKEN,
        "mcp_host": "127.0.0.1",
    }), encoding="utf-8")
    old_home = os.environ.get("TASKLANE_HOME")
    os.environ["TASKLANE_HOME"] = str(home)
    server, thread, url = start_server()

    yield url

    server.should_exit = True
    thread.join(timeout=10)
    if old_home is None:
        os.environ.pop("TASKLANE_HOME", None)
    else:
        os.environ["TASKLANE_HOME"] = old_home


def mcp_session_tools(url: str, token: str) -> set[str]:
    """Initialize a real MCP session with the given bearer token; return tool names."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def scenario() -> set[str]:
        headers = {"Authorization": f"Bearer {token}"}
        async with streamablehttp_client(f"{url}/mcp", headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return {t.name for t in (await session.list_tools()).tools}

    return asyncio.run(scenario())


def test_pair_returns_code_and_token(pairing_http):
    r = httpx.post(f"{pairing_http}/pair", json={"name": "laptop"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"client_id", "pairing_code", "token", "status"}
    assert body["status"] == "pending"
    assert body["client_id"].startswith("laptop-")
    assert len(body["pairing_code"]) == 8 and body["token"]


def test_pair_rejects_bad_bodies(pairing_http):
    assert httpx.post(f"{pairing_http}/pair", json={}).status_code == 400
    assert httpx.post(f"{pairing_http}/pair", json={"name": "   "}).status_code == 400
    assert httpx.post(f"{pairing_http}/pair", json={"name": 42}).status_code == 400
    assert httpx.post(f"{pairing_http}/pair", json=["laptop"]).status_code == 400
    assert httpx.post(f"{pairing_http}/pair", content=b"not json").status_code == 400
    assert httpx.post(f"{pairing_http}/pair", json={"name": "x" * 4096}).status_code == 400
    assert httpx.get(f"{pairing_http}/pair").status_code == 405


def test_paired_token_lifecycle(pairing_http):
    """pair -> 401 before approval -> approve -> MCP session works -> revoke -> 401."""
    from tasklane import pairing

    granted = httpx.post(f"{pairing_http}/pair", json={"name": "lifecycle"}).json()
    token = granted["token"]

    denied = httpx.post(f"{pairing_http}/mcp", json={}, headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 401, "unapproved paired token must be rejected"

    approved = pairing.approve_client(granted["pairing_code"])
    assert approved["client_id"] == granted["client_id"] and approved["approved_at"]

    tools = mcp_session_tools(pairing_http, token)
    assert PAIRING_TOOLS <= tools

    pairing.revoke_client(granted["client_id"])
    revoked = httpx.post(f"{pairing_http}/mcp", json={}, headers={"Authorization": f"Bearer {token}"})
    assert revoked.status_code == 401, "revoked paired token must be rejected"

    # the legacy app token is unaffected throughout
    assert PAIRING_TOOLS <= mcp_session_tools(pairing_http, TOKEN)


def test_pairing_admin_tools_over_mcp(pairing_http):
    """Approve and revoke a client purely through the MCP admin tools."""
    import json as jsonlib

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    granted = httpx.post(f"{pairing_http}/pair", json={"name": "tool-driven"}).json()
    rejected = httpx.post(f"{pairing_http}/pair", json={"name": "unwanted"}).json()

    def payload(result):
        if result.structuredContent is not None:
            p = result.structuredContent
            return p.get("result", p) if isinstance(p, dict) else p
        return jsonlib.loads(result.content[0].text)

    async def scenario():
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with streamablehttp_client(f"{pairing_http}/mcp", headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                pending = payload(await session.call_tool("pairing_requests", {}))
                codes = {p["pairing_code"] for p in pending}
                assert {granted["pairing_code"], rejected["pairing_code"]} <= codes

                approved = payload(await session.call_tool(
                    "approve_client", {"pairing_code": granted["pairing_code"]}))
                assert approved["client_id"] == granted["client_id"]

                refused = payload(await session.call_tool(
                    "reject_client", {"pairing_code": rejected["pairing_code"]}))
                assert refused["status"] == "rejected"

                clients = payload(await session.call_tool("list_clients", {}))
                ids = {c["client_id"] for c in clients}
                assert granted["client_id"] in ids and rejected["client_id"] not in ids

                revoked = payload(await session.call_tool(
                    "revoke_client", {"client_id": granted["client_id"]}))
                assert revoked["revoked_at"]

    asyncio.run(scenario())
    after = httpx.post(f"{pairing_http}/mcp", json={},
                       headers={"Authorization": f"Bearer {granted['token']}"})
    assert after.status_code == 401


def test_pair_pending_cap_returns_429(pairing_http):
    from tasklane import pairing

    created = []
    while len(pairing.pending_requests()) < pairing.PENDING_CAP:
        created.append(pairing.request_pairing(f"filler-{len(created)}"))
    try:
        r = httpx.post(f"{pairing_http}/pair", json={"name": "overflow"})
        assert r.status_code == 429
    finally:
        for req in created:
            pairing.reject_client(req["pairing_code"])


def test_pair_disabled_returns_404(tmp_path, monkeypatch):
    home = tmp_path / "tasklane-home"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "app_token": TOKEN,
        "mcp_host": "127.0.0.1",
        "pairing_enabled": False,
    }), encoding="utf-8")
    monkeypatch.setenv("TASKLANE_HOME", str(home))
    server, thread, url = start_server()
    try:
        assert httpx.post(f"{url}/pair", json={"name": "nope"}).status_code == 404
        # paired tokens are not consulted when pairing is disabled; legacy token still works
        assert httpx.get(f"{url}/health").status_code == 200
        assert httpx.post(f"{url}/mcp", json={},
                          headers={"Authorization": "Bearer not-the-token"}).status_code == 401
    finally:
        server.should_exit = True
        thread.join(timeout=10)
