"""Unit tests for the per-project registry and profile injection."""

import subprocess

import pytest

from tasklane.projects import (
    get_project,
    inject_project_profile,
    list_projects,
    register_project,
    render_profile_block,
)
from tasklane.worktree import job_prompt


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    return path


@pytest.fixture()
def tasklane_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TASKLANE_HOME", str(home))
    return home


@pytest.fixture()
def repo(tmp_path):
    return _git_repo(tmp_path / "repo")


def test_register_get_list_roundtrip(tasklane_home, repo):
    stored = register_project(
        str(repo), name="Demo", test_command=".venv/bin/python -m pytest -q",
        build_command="make build", docs=["CLAUDE.md"], base_branch="develop",
        default_model="claude-opus-4-8", merge_policy="auto",
    )
    assert stored["name"] == "Demo"
    assert stored["test_command"] == ".venv/bin/python -m pytest -q"
    assert stored["build_command"] == "make build"
    assert stored["base_branch"] == "develop"
    assert stored["merge_policy"] == "auto"

    fetched = get_project(str(repo))
    assert fetched is not None
    assert fetched["name"] == "Demo"
    assert fetched["docs"] == ["CLAUDE.md"]

    registry = list_projects()
    assert stored["path"] in registry
    assert registry[stored["path"]]["name"] == "Demo"


def test_register_is_update_not_duplicate(tasklane_home, repo):
    register_project(str(repo), name="One")
    register_project(str(repo), name="Two", test_command="pytest")
    registry = list_projects()
    assert len(registry) == 1
    assert get_project(str(repo))["name"] == "Two"


def test_register_lookup_from_subdir(tasklane_home, repo):
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    register_project(str(repo), name="Root")
    # a path inside the repo resolves to the same registry key
    assert get_project(str(sub)) is not None
    assert get_project(str(sub))["name"] == "Root"


def test_non_git_path_rejected(tasklane_home, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError):
        register_project(str(plain), name="Nope")


def test_get_unknown_project_returns_none(tasklane_home, repo):
    assert get_project(str(repo)) is None


def test_invalid_merge_policy_falls_back_to_manual(tasklane_home, repo):
    stored = register_project(str(repo), name="X", merge_policy="yolo")
    assert stored["merge_policy"] == "manual"


def test_render_profile_block_lists_only_existing_docs(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("rules", encoding="utf-8")
    profile = {
        "name": "Demo", "test_command": "pytest -q", "build_command": None,
        "docs": ["CLAUDE.md", "docs/adr/"], "base_branch": "main",
        "default_model": None, "merge_policy": "manual",
    }
    block = render_profile_block(profile, str(tmp_path))
    assert "Project profile:" in block
    assert "- Project: Demo" in block
    assert "verify with exactly this command): pytest -q" in block
    assert "MANDATORY: read CLAUDE.md" in block
    assert "docs/adr/" not in block  # not present in the worktree → not listed


def test_render_profile_block_empty_for_no_profile(tmp_path):
    assert render_profile_block(None, str(tmp_path)) == ""
    assert render_profile_block({}, str(tmp_path)) == ""


def _record(repo_path, original_path):
    return {
        "id": "job-1",
        "spec": {
            "repo": {"path": str(repo_path)},
            "request": {"type": "task", "title": "t", "body": "b"},
            "branch": {"mode": "new-branch", "base_branch": "main", "work_branch": "wb"},
            "delivery_mode": "direct-push",
            "metadata": {"workspace": {"isolated": True, "original_repo_path": str(original_path)}},
        },
    }


def test_worker_injection_end_to_end(tasklane_home, repo):
    (repo / "CLAUDE.md").write_text("project rules", encoding="utf-8")
    register_project(str(repo), name="Demo", test_command="pytest -q", docs=["CLAUDE.md"])

    # repo path is the (worktree) checkout; original_repo_path is the registered repo
    record = _record(repo, repo)
    injected = inject_project_profile(record)
    prompt = job_prompt(injected)

    assert "Project profile:" in prompt
    assert "MANDATORY: read CLAUDE.md" in prompt
    assert "verify with exactly this command): pytest -q" in prompt
    # original record is untouched (immutability)
    assert "project_profile" not in record["spec"]["metadata"]


def test_worker_injection_absent_when_unregistered(tasklane_home, repo):
    record = _record(repo, repo)
    injected = inject_project_profile(record)
    prompt = job_prompt(injected)
    assert "Project profile:" not in prompt


def test_worker_injection_falls_back_to_repo_path(tasklane_home, repo):
    register_project(str(repo), name="Demo")
    # no workspace metadata → look up spec.repo.path directly
    record = {
        "id": "job-2",
        "spec": {
            "repo": {"path": str(repo)},
            "request": {"type": "task", "title": "t", "body": "b"},
            "branch": {"mode": "new-branch"},
            "delivery_mode": "direct-push",
        },
    }
    injected = inject_project_profile(record)
    assert injected["spec"]["metadata"]["project_profile"]["name"] == "Demo"
