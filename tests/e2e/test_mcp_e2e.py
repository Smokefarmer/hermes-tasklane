"""E2E: the MCP control plane served over real HTTP — auth middleware (bearer
token, health, status page) plus lifecycle tools exercised through a real MCP
streamable-HTTP client session."""

import asyncio
import json
import os
import socket
import threading
import time

import httpx
import pytest
import yaml

pytestmark = pytest.mark.e2e

TOKEN = "e2e-test-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mcp_http(tmp_path_factory):
    """Start the real MCP app under uvicorn once for this module."""
    home = tmp_path_factory.mktemp("tasklane-home")
    (home / "config.yaml").write_text(yaml.safe_dump({
        "app_token": TOKEN,
        "mcp_host": "127.0.0.1",
        "poll_interval_seconds": 0.2,
    }), encoding="utf-8")
    old_home = os.environ.get("TASKLANE_HOME")
    os.environ["TASKLANE_HOME"] = str(home)

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

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)
    if old_home is None:
        os.environ.pop("TASKLANE_HOME", None)
    else:
        os.environ["TASKLANE_HOME"] = old_home


def test_health_is_public(mcp_http):
    r = httpx.get(f"{mcp_http}/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_mcp_rejects_missing_and_wrong_token(mcp_http):
    assert httpx.post(f"{mcp_http}/mcp", json={}).status_code == 401
    assert httpx.post(f"{mcp_http}/mcp", json={},
                      headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_status_page_requires_token(mcp_http):
    assert httpx.get(f"{mcp_http}/status").status_code == 401
    ok = httpx.get(f"{mcp_http}/status", params={"token": TOKEN})
    assert ok.status_code == 200 and "TaskLane" in ok.text


def tool_payload(result) -> dict | list:
    if result.structuredContent is not None:
        payload = result.structuredContent
        return payload.get("result", payload) if isinstance(payload, dict) else payload
    return json.loads(result.content[0].text)


def test_lifecycle_tools_over_mcp(mcp_http, git_repo):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def scenario():
        url = f"{mcp_http}/mcp"
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = {t.name for t in (await session.list_tools()).tools}
                assert {"create_task", "list_tasks", "get_task", "retry_task",
                        "cancel_task", "reconcile_jobs", "task_events"} <= tools

                created = tool_payload(await session.call_tool("create_task", {
                    "repo": str(git_repo), "title": "e2e mcp", "body": "body",
                    "delivery_mode": "report-only", "branch_mode": "detached-review",
                    "id": "e2e-mcp-1",
                }))
                assert created == {"id": "e2e-mcp-1", "state": "ready"}

                listed = tool_payload(await session.call_tool("list_tasks", {"state": "ready"}))
                assert any(j["id"] == "e2e-mcp-1" for j in listed)

                fetched = tool_payload(await session.call_tool("get_task", {"job_id": "e2e-mcp-1"}))
                assert fetched["state"] == "ready"

                report = tool_payload(await session.call_tool("reconcile_jobs", {}))
                assert set(report) == {"at", "orphans", "stale_locks_removed"}

                cancelled = tool_payload(await session.call_tool("cancel_task", {"job_id": "e2e-mcp-1"}))
                assert cancelled.get("state") == "failed" or cancelled.get("id") == "e2e-mcp-1"

                events = tool_payload(await session.call_tool("task_events", {"job_id": "e2e-mcp-1"}))
                assert any(e["event_type"] == "job_created" for e in events)

    asyncio.run(scenario())
