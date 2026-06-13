"""E2E: the /connections page over real HTTP — app_token gate, set + clear a
token through the browser form, masked status (never echoes the value)."""

import os
import socket
import threading
import time

import httpx
import pytest
import yaml

pytestmark = pytest.mark.e2e

TOKEN = "e2e-conn-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def conn_http(tmp_path_factory):
    home = tmp_path_factory.mktemp("conn-home")
    (home / "config.yaml").write_text(yaml.safe_dump({"app_token": TOKEN, "mcp_host": "127.0.0.1"}), encoding="utf-8")
    old = os.environ.get("TASKLANE_HOME")
    os.environ["TASKLANE_HOME"] = str(home)
    import uvicorn
    from tasklane.config import load_config
    from tasklane.mcp_server import build_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(load_config()), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    os.environ.pop("TASKLANE_HOME", None) if old is None else os.environ.__setitem__("TASKLANE_HOME", old)


def test_requires_app_token(conn_http):
    assert httpx.get(f"{conn_http}/connections").status_code == 401
    assert httpx.get(f"{conn_http}/connections", params={"token": "wrong"}).status_code == 401


def test_page_renders_with_token(conn_http):
    r = httpx.get(f"{conn_http}/connections", params={"token": TOKEN})
    assert r.status_code == 200
    assert "github" in r.text.lower() and "linear" in r.text.lower()


def test_set_and_clear_token_via_form(conn_http):
    # POST without token is rejected
    bad = httpx.post(f"{conn_http}/connections", data={"service": "github", "token": "ghp_x"},
                     follow_redirects=False)
    assert bad.status_code == 401

    # set a github token via the form
    r = httpx.post(f"{conn_http}/connections", data={
        "app_token": TOKEN, "service": "github", "token": "ghp_SECRET_VALUE", "default_repo": "o/r",
    }, follow_redirects=False)
    assert r.status_code == 302  # redirect back

    page = httpx.get(f"{conn_http}/connections", params={"token": TOKEN}).text
    assert "connected" in page and "o/r" in page
    assert "ghp_SECRET_VALUE" not in page  # masked, never echoed

    # clear it
    httpx.post(f"{conn_http}/connections", data={"app_token": TOKEN, "service": "github", "action": "clear"},
               follow_redirects=False)
    page2 = httpx.get(f"{conn_http}/connections", params={"token": TOKEN}).text
    assert "not connected" in page2.lower()
