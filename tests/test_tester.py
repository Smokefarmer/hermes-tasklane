"""Tester-role wiring: project profile + secret env resolution, the security
guarantee that prompts carry only env KEY NAMES (never values), the
test_deployment MCP tool, and the optional pipeline test stage."""

import os

import pytest

from tasklane.config import Config
from tasklane.projects import ProjectProfile, load_project_profile
from tasklane.specs import validate_job_spec
from tasklane.worker import inject_test_context, resolve_job_env
from tasklane.worktree import job_prompt


_SECRET_VALUES = {
    "TEST_USER": "alice_the_test_account",
    "TEST_PASSWORD": "P@ssw0rd-NEVER-LEAK-9173",
    "STAGING_URL": "https://staging.never-leak.example.com",
}


def _secret_file(tmp_path):
    path = tmp_path / "proj.env"
    path.write_text("\n".join(f"{k}={v}" for k, v in _SECRET_VALUES.items()), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _cfg_with_project(tmp_path, **profile_fields) -> Config:
    profile_fields.setdefault("env_file", str(_secret_file(tmp_path)))
    return Config(projects={"acme": profile_fields})


def _tester_record(role="test-local", project="acme", body="Log in and view the dashboard."):
    spec = validate_job_spec({
        "id": f"{role}-job",
        "project": project,
        "repo": {"path": "/tmp/demo-repo"},
        "request": {"type": "task", "title": "Verify deploy", "body": body},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
        "role": role,
    })
    return {"id": f"{role}-job", "spec": spec}


# --------------------------------------------------------------------------- #
# project profile
# --------------------------------------------------------------------------- #
def test_load_project_profile_from_config(tmp_path):
    cfg = _cfg_with_project(tmp_path, local_test_command="npm run dev", staging_url="https://s.example.com")
    profile = load_project_profile("acme", cfg)
    assert isinstance(profile, ProjectProfile)
    assert profile.local_test_command == "npm run dev"
    assert profile.staging_url == "https://s.example.com"


def test_load_project_profile_unknown_returns_none(tmp_path):
    assert load_project_profile("ghost", _cfg_with_project(tmp_path)) is None
    assert load_project_profile("", _cfg_with_project(tmp_path)) is None
    assert load_project_profile(None, Config()) is None


# --------------------------------------------------------------------------- #
# secret env resolution
# --------------------------------------------------------------------------- #
def test_resolve_job_env_loads_secret_values(tmp_path):
    cfg = _cfg_with_project(tmp_path)
    profile, env = resolve_job_env(_tester_record(), cfg)
    assert profile is not None
    assert env == _SECRET_VALUES


def test_resolve_job_env_no_project_is_empty(tmp_path):
    profile, env = resolve_job_env(_tester_record(project=None), Config())
    assert profile is None and env == {}


def test_resolve_job_env_profile_without_env_file(tmp_path):
    cfg = Config(projects={"acme": {"local_test_command": "make run"}})
    profile, env = resolve_job_env(_tester_record(), cfg)
    assert profile is not None and env == {}


# --------------------------------------------------------------------------- #
# SECURITY: prompt carries env KEY NAMES, provably NO env VALUES
# --------------------------------------------------------------------------- #
def test_tester_prompt_lists_key_names_and_hides_values(tmp_path):
    cfg = _cfg_with_project(tmp_path, local_test_command="npm run dev",
                            test_notes="Use the seeded tenant only.")
    record = _tester_record()
    profile, env = resolve_job_env(record, cfg)
    prepared = inject_test_context(record, profile, env)
    prompt = job_prompt(prepared)

    # KEY NAMES are present (sorted) so the agent knows what it can use.
    for name in _SECRET_VALUES:
        assert name in prompt
    # the non-secret project context is surfaced
    assert "npm run dev" in prompt
    assert "Use the seeded tenant only." in prompt

    # CORE GUARANTEE: no secret VALUE appears anywhere in the rendered prompt.
    for value in _SECRET_VALUES.values():
        assert value not in prompt


def test_inject_test_context_records_only_names(tmp_path):
    cfg = _cfg_with_project(tmp_path)
    record = _tester_record()
    profile, env = resolve_job_env(record, cfg)
    prepared = inject_test_context(record, profile, env)
    metadata = prepared["spec"]["metadata"]
    assert metadata["env_var_names"] == sorted(_SECRET_VALUES)
    # the metadata blob (which is persisted) must not contain any value
    import json
    blob = json.dumps(metadata)
    for value in _SECRET_VALUES.values():
        assert value not in blob


def test_tester_prompt_without_env_still_functional():
    prompt = job_prompt(_tester_record(project=None))
    assert "none injected for this job" in prompt
    assert "RUNNING it" in prompt  # mission preserved


# --------------------------------------------------------------------------- #
# test_deployment MCP tool
# --------------------------------------------------------------------------- #
def test_test_deployment_staging_spec(monkeypatch, tmp_path):
    from tasklane import mcp_server
    monkeypatch.setattr(mcp_server, "repo_path_allowed", lambda *_: True)

    captured = {}
    monkeypatch.setattr(mcp_server, "_store", lambda: _FakeStore(captured))

    result = mcp_server.test_deployment("/tmp/repo", mode="staging", flows="login\ncheckout")
    spec = validate_job_spec(captured["spec"])
    assert spec["role"] == "test-staging"
    assert spec["delivery_mode"] == "report-only"
    assert spec["branch"]["mode"] == "detached-review"
    assert "login" in spec["request"]["body"] and "checkout" in spec["request"]["body"]
    assert result["role"] == "test-staging" and result["mode"] == "staging"


def test_test_deployment_local_spec(monkeypatch):
    from tasklane import mcp_server
    monkeypatch.setattr(mcp_server, "repo_path_allowed", lambda *_: True)
    captured = {}
    monkeypatch.setattr(mcp_server, "_store", lambda: _FakeStore(captured))

    mcp_server.test_deployment("/tmp/repo", mode="local", project="acme")
    spec = validate_job_spec(captured["spec"])
    assert spec["role"] == "test-local"
    assert spec["project"] == "acme"
    assert spec["delivery_mode"] == "report-only"


def test_test_deployment_rejects_unknown_mode(monkeypatch):
    from tasklane import mcp_server
    monkeypatch.setattr(mcp_server, "repo_path_allowed", lambda *_: True)
    monkeypatch.setattr(mcp_server, "_store", lambda: _FakeStore({}))
    # @audited swallows the raise into an error dict
    result = mcp_server.test_deployment("/tmp/repo", mode="prod")
    assert "error" in result and "unknown mode" in result["error"]


class _FakeStore:
    def __init__(self, captured):
        self._captured = captured

    def put(self, spec):
        self._captured["spec"] = spec
        return {"id": spec.get("id") or "generated-id", "state": "ready"}


# --------------------------------------------------------------------------- #
# pipeline test stage chains onto review
# --------------------------------------------------------------------------- #
def test_pipeline_test_stage_chains_onto_review():
    from tasklane.mcp_server import _pipeline_stage_specs

    specs = _pipeline_stage_specs(
        "feat-y", "/tmp/repo", "Feature Y", "build feature y",
        stages=["plan", "implement", "review", "test"],
        work_branch="tasklane/feat-y", base_branch="main",
        pr_target="main", delivery_mode="pull-request",
    )
    assert [s["id"] for s in specs][-1] == "feat-y-test"

    test_spec = validate_job_spec(specs[-1])
    assert test_spec["role"] == "test-local"
    assert test_spec["delivery_mode"] == "report-only"
    assert test_spec["branch"]["mode"] == "detached-review"
    # detached on the WORK branch so it checks out the implemented tip
    assert test_spec["branch"]["base_branch"] == "tasklane/feat-y"
    # chained onto the review stage with context_from
    assert test_spec["dependencies"] == ["feat-y-review"]
    assert test_spec["context_from"] == ["feat-y-review"]


def test_pipeline_test_stage_carries_project_for_creds():
    from tasklane.mcp_server import _pipeline_stage_specs

    specs = _pipeline_stage_specs(
        "feat-p", "/tmp/repo", "Feature P", "body",
        stages=["implement", "review", "test"],
        work_branch="wb", base_branch="main", pr_target="main",
        delivery_mode="pull-request", project="acme",
    )
    by_id = {s["id"]: s for s in specs}
    # only the test stage gets the project (so only it receives injected secrets)
    assert by_id["feat-p-test"]["project"] == "acme"
    assert "project" not in by_id["feat-p-implement"]
    assert "project" not in by_id["feat-p-review"]


def test_pipeline_default_omits_test_stage():
    from tasklane.mcp_server import _pipeline_stage_specs

    specs = _pipeline_stage_specs(
        "feat-z", "/tmp/repo", "Feature Z", "body",
        stages=["plan", "implement", "review"],
        work_branch="wb", base_branch="main", pr_target="main", delivery_mode="pull-request",
    )
    assert "feat-z-test" not in [s["id"] for s in specs]
