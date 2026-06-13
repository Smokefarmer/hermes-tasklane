"""Tests for the integration MCP tools: bridges, issue->task, audit redaction."""

import subprocess

import pytest

from tasklane import integrations, mcp_server
from tasklane.mcp import integration_tools
from tasklane.paths import audit_log_path
from tasklane.store import JobStore


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path / "home"))
    return tmp_path


def _git_repo(tmp_path, name="repo") -> str:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return str(repo)


# --------------------------------------------------------------------------- #
# audit redaction — the token must never reach the log
# --------------------------------------------------------------------------- #
def test_set_integration_token_redacts_value(home):
    mcp_server.set_integration_token(service="github", token="ghp_DO_NOT_LEAK", default_repo="o/r")
    audit = audit_log_path().read_text(encoding="utf-8")
    assert "set_integration_token" in audit
    assert "ghp_DO_NOT_LEAK" not in audit  # value redacted
    # and the stored status never returns the value either
    assert "ghp_DO_NOT_LEAK" not in str(mcp_server.integration_status())


# --------------------------------------------------------------------------- #
# bridge: Linear -> GitHub
# --------------------------------------------------------------------------- #
def test_linear_to_github_issue(home, monkeypatch):
    monkeypatch.setattr(integrations, "linear_get_issue", lambda i: {
        "identifier": "ENG-7", "title": "Crash on save", "description": "stack trace...",
        "url": "https://lin/ENG-7"})
    created = {}
    monkeypatch.setattr(integrations, "github_create_issue",
                        lambda repo, title, body, labels=None: created.update(
                            repo=repo, title=title, body=body, labels=labels) or
                            {"number": 12, "url": "https://gh/i/12", "title": title, "state": "open"})
    integrations.set_token("github", "ghp_x", default_repo="o/r")
    out = integration_tools.linear_to_github_issue("ENG-7")
    assert out["github_issue"]["number"] == 12
    assert created["title"] == "[ENG-7] Crash on save"
    assert "https://lin/ENG-7" in created["body"]  # back-link
    assert created["repo"] == "o/r"  # default repo used


# --------------------------------------------------------------------------- #
# bridge: GitHub issue -> TaskLane job
# --------------------------------------------------------------------------- #
def test_github_issue_to_task_creates_job(home, tmp_path, monkeypatch):
    repo_path = _git_repo(tmp_path)
    integrations.set_token("github", "ghp_x", default_repo="o/r")
    monkeypatch.setattr(integrations, "github_get_issue", lambda repo, number: {
        "number": 42, "title": "Add retries", "body": "we need backoff",
        "url": "https://gh/o/r/issues/42", "state": "open", "labels": []})

    out = integration_tools.github_issue_to_task(number=42, repo_path=repo_path, work_branch="tasklane/issue-42")
    record = JobStore().get(out["id"])
    assert record["state"] == "ready"
    assert record["spec"]["repo"]["path"] == repo_path           # LOCAL path, not owner/name
    assert record["spec"]["source"]["issue_number"] == 42
    assert record["spec"]["source"]["github_issue"] == "https://gh/o/r/issues/42"
    assert "Closes #42" in record["spec"]["request"]["body"]
    assert record["spec"]["branch"]["work_branch"] == "tasklane/issue-42"


def test_github_issue_to_task_enforces_allowlist(home, tmp_path, monkeypatch):
    repo_path = _git_repo(tmp_path)
    integrations.set_token("github", "ghp_x", default_repo="o/r")
    monkeypatch.setattr(integrations, "github_get_issue", lambda repo, number: {"number": 1, "title": "t"})
    from tasklane.config import Config
    monkeypatch.setattr(integration_tools, "_cfg", lambda: Config(repos_allowlist=["/elsewhere"]))
    out = integration_tools.github_issue_to_task(number=1, repo_path=repo_path, work_branch="wb")
    assert "error" in out and "allowlist" in out["error"]


def test_github_tool_uses_default_repo(home, monkeypatch):
    integrations.set_token("github", "ghp_x", default_repo="acme/app")
    seen = {}
    monkeypatch.setattr(integrations, "github_list_issues",
                        lambda repo, state, label, limit: seen.update(repo=repo) or [])
    integration_tools.github_list_issues()  # no repo arg → default
    assert seen["repo"] == "acme/app"


def test_github_tool_no_repo_no_default_errors(home):
    integrations.set_token("github", "ghp_x")  # no default_repo
    out = integration_tools.github_list_issues()  # audited → error dict
    assert "error" in out and "no repo" in out["error"]
