"""Unit tests for GitHub + Linear integrations (HTTP seam stubbed, no network)."""

import os
import stat
import subprocess

import pytest

from tasklane import integrations


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture()
def stub_http(monkeypatch):
    """Replace the single HTTP seam with a scripted (status, body) responder."""
    calls = []
    script = {"resp": (200, {})}

    def fake(method, url, *, headers, body=None):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        resp = script["resp"]
        return resp(method, url, body) if callable(resp) else resp

    monkeypatch.setattr(integrations, "_http_json", fake)
    return calls, script


# --------------------------------------------------------------------------- #
# credential store
# --------------------------------------------------------------------------- #
def test_set_get_clear_token(home):
    integrations.set_token("github", "ghp_secret", default_repo="o/r")
    assert integrations.get_token("github") == "ghp_secret"
    assert integrations.get_meta("github", "default_repo") == "o/r"
    assert integrations.clear_token("github") is True
    assert integrations.get_token("github") is None


def test_token_file_is_600(home):
    integrations.set_token("linear", "lin_abc")
    assert stat.S_IMODE(os.stat(integrations.integrations_path()).st_mode) == 0o600


def test_status_masks_token_never_leaks(home):
    integrations.set_token("github", "ghp_supersecretvalue", default_repo="o/r")
    st = integrations.status()
    assert st["github"]["connected"] is True
    assert "supersecret" not in str(st)  # value never present
    assert st["github"]["default_repo"] == "o/r"
    assert st["linear"] == {"connected": False}


def test_unknown_service_rejected(home):
    with pytest.raises(ValueError, match="unknown service"):
        integrations.set_token("gitlab", "x")


def test_empty_token_rejected(home):
    with pytest.raises(ValueError, match="token is required"):
        integrations.set_token("github", "   ")


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #
def test_github_create_issue_happy(home, stub_http):
    calls, script = stub_http
    integrations.set_token("github", "ghp_x")
    script["resp"] = (201, {"number": 7, "html_url": "https://gh/i/7", "title": "Bug", "state": "open"})
    out = integrations.github_create_issue("owner/repo", "Bug", "details", ["bug"])
    assert out == {"number": 7, "url": "https://gh/i/7", "title": "Bug", "state": "open"}
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["body"]["labels"] == ["bug"]
    assert "Bearer ghp_x" in calls[-1]["headers"]["Authorization"]


def test_github_create_issue_not_connected(home, stub_http):
    with pytest.raises(ValueError, match="GitHub is not connected"):
        integrations.github_create_issue("o/r", "t")


def test_github_create_issue_bad_repo(home, stub_http):
    integrations.set_token("github", "ghp_x")
    with pytest.raises(ValueError, match="owner/name"):
        integrations.github_create_issue("notarepo", "t")


def test_github_create_issue_requires_title(home, stub_http):
    integrations.set_token("github", "ghp_x")
    with pytest.raises(ValueError, match="title is required"):
        integrations.github_create_issue("o/r", "  ")


def test_github_create_issue_api_error(home, stub_http):
    _, script = stub_http
    integrations.set_token("github", "ghp_x")
    script["resp"] = (422, {"message": "Validation failed"})
    with pytest.raises(ValueError, match="422.*Validation failed"):
        integrations.github_create_issue("o/r", "t")


def test_github_list_filters_out_prs(home, stub_http):
    _, script = stub_http
    integrations.set_token("github", "ghp_x")
    script["resp"] = (200, [
        {"number": 1, "title": "issue", "state": "open", "html_url": "u1", "labels": [{"name": "bug"}]},
        {"number": 2, "title": "a PR", "state": "open", "html_url": "u2", "labels": [], "pull_request": {"url": "x"}},
    ])
    out = integrations.github_list_issues("o/r")
    assert [i["number"] for i in out] == [1]
    assert out[0]["labels"] == ["bug"]


def test_github_get_issue(home, stub_http):
    _, script = stub_http
    integrations.set_token("github", "ghp_x")
    script["resp"] = (200, {"number": 5, "title": "T", "body": "B", "state": "open",
                            "html_url": "u", "labels": [{"name": "x"}]})
    out = integrations.github_get_issue("o/r", 5)
    assert out["number"] == 5 and out["body"] == "B" and out["labels"] == ["x"]


# --------------------------------------------------------------------------- #
# Linear
# --------------------------------------------------------------------------- #
def test_linear_get_issue_happy(home, stub_http):
    _, script = stub_http
    integrations.set_token("linear", "lin_x")
    script["resp"] = (200, {"data": {"issue": {
        "id": "abc", "identifier": "ENG-1", "title": "Fix", "description": "d",
        "url": "https://lin/ENG-1", "state": {"name": "Todo"}}}})
    out = integrations.linear_get_issue("ENG-1")
    assert out["identifier"] == "ENG-1" and out["state"] == "Todo"


def test_linear_not_found(home, stub_http):
    _, script = stub_http
    integrations.set_token("linear", "lin_x")
    script["resp"] = (200, {"data": {"issue": None}})
    with pytest.raises(ValueError, match="not found"):
        integrations.linear_get_issue("ENG-999")


def test_linear_graphql_error(home, stub_http):
    _, script = stub_http
    integrations.set_token("linear", "lin_x")
    script["resp"] = (200, {"errors": [{"message": "bad query"}]})
    with pytest.raises(ValueError, match="GraphQL error"):
        integrations.linear_list_issues()


def test_linear_not_connected(home, stub_http):
    with pytest.raises(ValueError, match="Linear is not connected"):
        integrations.linear_list_issues()
