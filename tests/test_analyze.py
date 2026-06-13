"""Unit tests for the architecture-audit feature: the analyze-role spec builder
and analyze_project MCP tool, plus the rendered analyze-role prompt contract."""

import pytest

from tasklane.specs import validate_job_spec
from tasklane.worktree import job_prompt


# --------------------------------------------------------------------------- #
# spec builder
# --------------------------------------------------------------------------- #
def test_analyze_job_spec_defaults_validate():
    from tasklane.mcp_server import _analyze_job_spec

    spec = validate_job_spec(_analyze_job_spec("/tmp/repo", base_branch="main", id=None))
    assert spec["role"] == "analyze"
    assert spec["branch"]["mode"] == "new-branch"
    assert spec["branch"]["base_branch"] == "main"
    assert spec["branch"]["work_branch"] == "tasklane/architecture-review"
    assert spec["delivery_mode"] == "direct-push"
    assert spec["request"]["type"] == "refactor-large"


def test_analyze_job_spec_id_derives_branch_and_id():
    from tasklane.mcp_server import _analyze_job_spec

    spec = validate_job_spec(_analyze_job_spec("/tmp/repo", base_branch="develop", id="audit-1"))
    assert spec["id"] == "audit-1"
    assert spec["branch"]["work_branch"] == "tasklane/architecture-review-audit-1"
    assert spec["branch"]["base_branch"] == "develop"


# --------------------------------------------------------------------------- #
# analyze_project MCP tool
# --------------------------------------------------------------------------- #
def test_analyze_project_creates_job(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path))
    from tasklane import mcp_server
    from tasklane.store import JobStore

    result = mcp_server.analyze_project(repo="/tmp/some-repo", base_branch="main")
    assert "error" not in result
    job_id = result["id"]

    record = JobStore(root=tmp_path / "jobs").get(job_id)
    assert record is not None
    spec = record["spec"]
    assert spec["role"] == "analyze"
    assert spec["delivery_mode"] == "direct-push"
    assert spec["branch"]["mode"] == "new-branch"
    assert spec["branch"]["work_branch"] == "tasklane/architecture-review"
    assert spec["request"]["type"] == "refactor-large"


def test_analyze_project_rejects_disallowed_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path))
    from tasklane import config, mcp_server

    cfg = config.Config(repos_allowlist=["/allowed/only"])
    monkeypatch.setattr(mcp_server, "_cfg", lambda: cfg)

    result = mcp_server.analyze_project(repo="/tmp/forbidden-repo")
    assert "error" in result
    assert "allowlist" in result["error"]


# --------------------------------------------------------------------------- #
# rendered analyze-role prompt
# --------------------------------------------------------------------------- #
def _analyze_record():
    spec = validate_job_spec({
        "id": "analyze-job",
        "repo": {"path": "/tmp/demo-repo"},
        "request": {"type": "refactor-large", "title": "Architecture review", "body": "Audit it."},
        "branch": {"mode": "new-branch", "base_branch": "main",
                   "work_branch": "tasklane/architecture-review"},
        "delivery_mode": "direct-push",
        "role": "analyze",
    })
    return {"id": "analyze-job", "spec": spec}


def test_analyze_prompt_has_four_passes_and_output_contract():
    prompt = job_prompt(_analyze_record())
    lower = prompt.lower()
    # the four explicit passes
    assert "pass 1" in lower and "map" in lower
    assert "pass 2" in lower and "intent" in lower
    assert "pass 3" in lower and "patterns" in lower
    assert "pass 4" in lower and "accretion" in lower
    # output contract: review document + ADR drafts + proposed_tasks block
    assert "docs/architecture-review.md" in prompt
    assert "docs/adr/proposed/" in prompt
    assert "```proposed_tasks" in prompt
    # placeholders are all filled
    for placeholder in ("{job_id}", "{body}", "{branch_contract}", "{scope_contract}",
                        "{delivery_contract}", "{upstream_context}"):
        assert placeholder not in prompt
