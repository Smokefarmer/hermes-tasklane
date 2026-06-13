"""E2E: the MCP worktree fix-tools (get_diff, list_dir, read_file, write_file,
apply_patch, exec, git, run_tests) exercised over a real streamable-HTTP MCP
session against a blocked job whose dirty worktree was kept for repair, ending
with retry_task re-queueing the job."""

import asyncio
import threading
import time
from pathlib import Path

import pytest

from tasklane.store import JobStore
from tasklane.worker import run_job

from .conftest import fake_calls, job_spec
from .test_mcp_e2e import TOKEN, free_port, tool_payload

pytestmark = pytest.mark.e2e

JOB_ID = "e2e-fixtools"


@pytest.fixture()
def store(tasklane_home):
    s = JobStore()
    s.ensure_dirs()
    return s


@pytest.fixture()
def blocked_job(store, fast_cfg, fake_claude, git_repo, monkeypatch) -> dict:
    """A job whose agent left the worktree dirty: blocked, worktree kept on disk."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "dirty")
    store.put(job_spec(git_repo, JOB_ID))
    record = store.claim_next(owner="worker-e2e")
    assert record is not None
    run_job(store, fast_cfg, record)

    final = store.get(JOB_ID)
    assert final["state"] == "blocked"
    assert "uncommitted changes" in final["last_error"]
    assert fake_calls(fake_claude) == 2  # main pass + failed repair pass
    wt = Path(final["worktree"]["worktree_path"])
    assert wt.is_dir() and (wt / "JUNK.txt").exists()
    return final


@pytest.fixture()
def mcp_http(tasklane_home):
    """The real MCP app under uvicorn, bound to this test's TASKLANE_HOME."""
    import uvicorn

    from tasklane import mcp_server
    from tasklane.config import load_config

    # FastMCP caches its StreamableHTTPSessionManager and the manager's run()
    # is one-shot; clear the cache so this server (and any started later in
    # the same pytest process, e.g. by test_mcp_e2e) gets a fresh manager.
    mcp_server.mcp._session_manager = None
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(mcp_server.build_app(load_config()), host="127.0.0.1",
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
    mcp_server.mcp._session_manager = None


def test_fix_tools_inspect_repair_and_requeue(mcp_http, blocked_job, store):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    wt = Path(blocked_job["worktree"]["worktree_path"])

    async def scenario():
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with streamablehttp_client(f"{mcp_http}/mcp", headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name: str, **arguments):
                    return tool_payload(await session.call_tool(name, {"job_id": JOB_ID, **arguments}))

                # get_diff: the dirty (untracked) file shows up in git status
                diff = await call("get_diff")
                assert diff["worktree"] == str(wt)
                assert "JUNK.txt" in diff["status"]["stdout"]
                assert diff["diff"]["returncode"] == 0 and diff["staged_diff"]["returncode"] == 0

                # list_dir: worktree root contains the repo files plus the junk
                listing = await call("list_dir")
                names = {e["name"] for e in listing["entries"]}
                assert {"README.md", "JUNK.txt"} <= names

                # read_file: real repo content comes back untruncated
                readme = await call("read_file", path="README.md")
                assert "e2e fixture repo" in readme["content"]
                assert readme["truncated"] is False

                # path-escape guard: the audited wrapper converts the ValueError
                escape = await call("read_file", path="../outside")
                assert "path escapes worktree" in escape["error"]

                # write_file: creates the file on disk inside the worktree
                note = "fix plan\n"
                written = await call("write_file", path="NOTES.md", content=note)
                assert written["bytes_written"] == len(note)
                assert (wt / "NOTES.md").read_text(encoding="utf-8") == note

                # apply_patch: a unified diff against the note file lands
                patch = ("--- a/NOTES.md\n"
                         "+++ b/NOTES.md\n"
                         "@@ -1 +1,2 @@\n"
                         " fix plan\n"
                         "+patched\n")
                applied = await call("apply_patch", unified_diff=patch)
                assert applied["returncode"] == 0, applied["stderr"]
                assert (wt / "NOTES.md").read_text(encoding="utf-8") == "fix plan\npatched\n"

                # exec: clean the worktree the way an operator would
                cleaned = await call("exec", command="rm JUNK.txt NOTES.md")
                assert cleaned["returncode"] == 0, cleaned["stderr"]
                assert not (wt / "JUNK.txt").exists()

                # git: status is clean again
                status = await call("git", args="status --short")
                assert status["returncode"] == 0
                assert status["stdout"].strip() == ""

                # run_tests: explicit trivial command runs in the worktree
                tests = await call("run_tests", command="true")
                assert tests["returncode"] == 0

                # retry_task: the repaired job is re-queued
                retried = await call("retry_task")
                assert retried == {"id": JOB_ID, "state": "ready"}

    asyncio.run(scenario())
    assert store.get(JOB_ID)["state"] == "ready"
