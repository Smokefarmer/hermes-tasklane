"""Unit tests for pipeline plumbing: context_from spec field, upstream-context
injection, per-repo claim serialization, and pipeline stage-spec generation."""

import pytest

from tasklane.specs import validate_job_spec
from tasklane.store import JobStore
from tasklane.worker import inject_upstream_context
from tasklane.worktree import job_prompt


def make_spec(job_id: str, repo_path: str = "/tmp/example-repo", **extra):
    spec = {
        "id": job_id,
        "repo": {"path": repo_path},
        "request": {"type": "task", "title": f"t-{job_id}", "body": "b"},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
    }
    spec.update(extra)
    return spec


@pytest.fixture()
def store(tmp_path):
    s = JobStore(root=tmp_path / "jobs")
    s.ensure_dirs()
    return s


# --------------------------------------------------------------------------- #
# context_from spec field
# --------------------------------------------------------------------------- #
def test_context_from_normalized_list_and_csv():
    spec = validate_job_spec(make_spec("a", context_from=["x", "y", "x", ""]))
    assert spec["context_from"] == ["x", "y"]
    spec = validate_job_spec(make_spec("a", context_from="x, y"))
    assert spec["context_from"] == ["x", "y"]


def test_context_from_defaults_empty():
    assert validate_job_spec(make_spec("a"))["context_from"] == []


def test_context_from_rejects_garbage():
    with pytest.raises(ValueError, match="context_from"):
        validate_job_spec(make_spec("a", context_from={"not": "a list"}))


# --------------------------------------------------------------------------- #
# upstream-context injection + prompt rendering
# --------------------------------------------------------------------------- #
def test_inject_upstream_context_renders_in_prompt(store):
    upstream = store.put(make_spec("up-1"))
    store.claim_next(owner="w")
    store.complete(upstream["id"], result={"final_response": "THE PLAN: do X then Y"})

    record = store.put(make_spec("down-1", context_from=["up-1"]))
    prepared = inject_upstream_context(store, record)
    entries = prepared["spec"]["metadata"]["upstream_context"]
    assert entries[0]["job_id"] == "up-1"
    assert entries[0]["state"] == "completed"
    assert "THE PLAN" in entries[0]["final_response"]

    prompt = job_prompt(prepared)
    assert "upstream job up-1" in prompt
    assert "THE PLAN: do X then Y" in prompt


def test_inject_upstream_context_notes_missing_job(store):
    record = store.put(make_spec("down-2", context_from=["ghost"]))
    prepared = inject_upstream_context(store, record)
    assert prepared["spec"]["metadata"]["upstream_context"][0]["note"] == "upstream job not found"


def test_inject_upstream_context_noop_without_field(store):
    record = store.put(make_spec("plain"))
    assert inject_upstream_context(store, record) is record


def test_inject_upstream_context_truncates_long_responses(store):
    upstream = store.put(make_spec("up-long"))
    store.claim_next(owner="w")
    store.complete(upstream["id"], result={"final_response": "x" * 10_000})
    record = store.put(make_spec("down-3", context_from=["up-long"]))
    prepared = inject_upstream_context(store, record)
    assert len(prepared["spec"]["metadata"]["upstream_context"][0]["final_response"]) == 4000


# --------------------------------------------------------------------------- #
# per-repo claim serialization
# --------------------------------------------------------------------------- #
def test_claim_skips_repos_already_running(store):
    store.put(make_spec("repo-a-1", repo_path="/tmp/repo-a"))
    store.put(make_spec("repo-a-2", repo_path="/tmp/repo-a"))
    store.put(make_spec("repo-b-1", repo_path="/tmp/repo-b"))

    first = store.claim_next(owner="w")
    key = first["spec"]["repo"]["key"]
    assert key  # normalizer derives repo://<path>

    second = store.claim_next(owner="w", active_repo_keys={key})
    assert second is not None and second["spec"]["repo"]["key"] != key

    # both repos busy → nothing claimable even though repo-a-2 is ready
    third = store.claim_next(owner="w", active_repo_keys={key, second["spec"]["repo"]["key"]})
    assert third is None


# --------------------------------------------------------------------------- #
# pipeline stage-spec generation
# --------------------------------------------------------------------------- #
def test_pipeline_stage_specs_chain_and_validate():
    from tasklane.mcp_server import _pipeline_stage_specs

    specs = _pipeline_stage_specs(
        "feat-x", "/tmp/repo", "Feature X", "build feature x",
        stages=["plan", "implement", "review"],
        work_branch="tasklane/feat-x", base_branch="main",
        pr_target="main", delivery_mode="pull-request",
    )
    assert [s["id"] for s in specs] == ["feat-x-plan", "feat-x-implement", "feat-x-review"]

    plan, implement, review = (validate_job_spec(s) for s in specs)
    assert plan["delivery_mode"] == "report-only" and plan["dependencies"] == []
    assert implement["dependencies"] == ["feat-x-plan"]
    assert implement["context_from"] == ["feat-x-plan"]
    assert implement["branch"]["work_branch"] == "tasklane/feat-x"
    assert review["dependencies"] == ["feat-x-implement"]
    assert review["delivery_mode"] == "report-only"
    # review checks out the work branch tip (detached), not the base branch
    assert review["branch"]["base_branch"] == "tasklane/feat-x"


def test_pipeline_stage_specs_implement_only():
    from tasklane.mcp_server import _pipeline_stage_specs

    specs = _pipeline_stage_specs(
        "solo", "/tmp/repo", "Solo", "body", stages=["implement"],
        work_branch="wb", base_branch="main", pr_target=None, delivery_mode="direct-push",
    )
    assert len(specs) == 1
    only = validate_job_spec(specs[0])
    assert only["dependencies"] == [] and only["context_from"] == []
