"""Guard tests: mutating fix tools must REFUSE when a job has no live worktree
(never fall back to the user's original checkout); read-only tools may fall back
but must label the target as the original repo."""

import subprocess

import pytest

from tasklane import mcp_server
from tasklane.store import JobStore


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path / "home"))
    return tmp_path


def _real_repo(tmp_path) -> str:
    repo = tmp_path / "real-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("real checkout\n")
    return str(repo)


def _put_job_without_worktree(tmp_path, job_id="no-wt"):
    repo = _real_repo(tmp_path)
    store = JobStore()
    store.ensure_dirs()
    store.put({
        "id": job_id,
        "repo": {"path": repo},
        "request": {"type": "task", "title": "t", "body": "b"},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
    })
    return repo


MUTATING_CALLS = [
    ("write_file", lambda jid: mcp_server.write_file(jid, "HACK.txt", "x")),
    ("apply_patch", lambda jid: mcp_server.apply_patch(jid, "--- a\n+++ b\n")),
    ("exec", lambda jid: mcp_server.exec(jid, "touch HACK.txt")),
    ("git", lambda jid: mcp_server.git(jid, "status")),
    ("run_tests", lambda jid: mcp_server.run_tests(jid, "true")),
]


@pytest.mark.parametrize("name,call", MUTATING_CALLS, ids=[c[0] for c in MUTATING_CALLS])
def test_mutating_tool_refuses_without_worktree(home, tmp_path, name, call):
    repo = _put_job_without_worktree(tmp_path)
    result = call("no-wt")
    # the audited wrapper turns the refusal into an {"error": ...} payload
    assert isinstance(result, dict) and "error" in result, f"{name} did not refuse"
    assert "no live worktree" in result["error"] or "refusing" in result["error"]
    # the original checkout was NOT mutated
    assert not (tmp_path / "real-repo" / "HACK.txt").exists()


def test_read_tools_fall_back_and_label_original(home, tmp_path):
    _put_job_without_worktree(tmp_path)
    # read_file works against the original repo, but says so
    out = mcp_server.read_file("no-wt", "README.md")
    assert out["target"] == "original-repo"
    assert "real checkout" in out["content"]
    listing = mcp_server.list_dir("no-wt", ".")
    assert listing["target"] == "original-repo"


def test_worktree_dir_strict_vs_readable(home, tmp_path):
    _put_job_without_worktree(tmp_path)
    record = JobStore().get("no-wt")
    with pytest.raises(FileNotFoundError, match="refusing to operate"):
        mcp_server._worktree_dir(record)
    path, is_original = mcp_server._readable_dir(record)
    assert is_original is True and path.name == "real-repo"
