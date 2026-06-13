"""Unit tests for role-based stage prompt templates: the loader (packaging +
operator override), the `role` spec field, and role-aware job_prompt rendering.
"""

import pytest

from tasklane.prompts import ROLES, load_template, normalize_role
from tasklane.specs import validate_job_spec
from tasklane.worktree import job_prompt


# Frozen snapshot of the generic (no-role) prompt — guards backward compatibility.
_GENERIC_SNAPSHOT = """You are running a TaskLane autonomous coding job.

Pipeline contract:
1. Plan the concrete steps briefly before editing.
2. Implement the requested change in the target repo.
3. Review your own diff for security, correctness, and clean coding.
4. Fix review findings, with up to 3 implementation/review loops if needed.
5. Verify with the strongest relevant tests or checks.
6. Deliver according to the delivery mode.

Job ID: snap-job
Project: demo
Repo path: /tmp/demo-repo
Request type: task-small
Title: Snap title
Task:
Do the snap thing.

Branch contract:
- mode: new-branch
- base_branch: main
- work_branch: tasklane/snap
- pr_target: main
- delivery_mode: pull-request

Scope contract:
- allowed_paths: (none declared)
- denied_paths: (none declared)
- allow_unlisted_paths: True

Acceptance criteria:

Operational rules:
- Use the repo path above; do not work outside the target repo.
- If branch mode is new-branch, create/switch to work_branch from base_branch.
- If delivery mode is pull-request, commit, push work_branch, and open a PR against pr_target.
- If delivery mode is direct-push, commit and push the target work_branch only.
- If delivery mode is report-only, do not commit or push; return findings only.
- Never modify denied paths. If allow_unlisted_paths is false, modify only allowed_paths.
- End with changed files, verification performed, delivery URL/branch, and residual risks."""


def _snapshot_record():
    spec = validate_job_spec({
        "id": "snap-job",
        "project": "demo",
        "repo": {"path": "/tmp/demo-repo"},
        "request": {"type": "task", "title": "Snap title", "body": "Do the snap thing."},
        "branch": {"mode": "new-branch", "base_branch": "main",
                   "work_branch": "tasklane/snap", "pr_target": "main"},
        "delivery_mode": "pull-request",
    })
    return {"id": "snap-job", "spec": spec}


def _role_record(role: str, body: str = "Do the snap thing."):
    spec = validate_job_spec({
        "id": f"{role}-job",
        "repo": {"path": "/tmp/demo-repo"},
        "request": {"type": "task", "title": "Snap title", "body": body},
        "branch": {"mode": "detached-review", "base_branch": "main"},
        "delivery_mode": "report-only",
        "role": role,
    })
    return {"id": f"{role}-job", "spec": spec}


# --------------------------------------------------------------------------- #
# template loading
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["plan", "implement", "review", "fix", "audit"])
def test_load_template_for_every_role(role):
    template = load_template(role)
    assert template is not None
    assert template.strip()
    # placeholders the renderer fills must be present in the shipped template
    assert "{job_id}" in template
    assert "{body}" in template


def test_roles_constant_matches_templates():
    assert set(ROLES) == {"plan", "implement", "review", "fix", "audit"}


def test_load_template_normalizes_role():
    assert load_template(" Review ") == load_template("review")


def test_load_template_unknown_role_returns_none():
    assert load_template("architect") is None
    assert load_template("") is None
    assert load_template(None) is None


def test_normalize_role_helper():
    assert normalize_role(" Plan ") == "plan"
    assert normalize_role(None) == ""


# --------------------------------------------------------------------------- #
# operator override precedence
# --------------------------------------------------------------------------- #
def test_operator_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path))
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "review.md").write_text("OPERATOR OVERRIDE for review {body}", encoding="utf-8")

    assert load_template("review") == "OPERATOR OVERRIDE for review {body}"
    # roles without an override file still load the packaged default
    assert "OPERATOR OVERRIDE" not in (load_template("plan") or "")


def test_operator_override_absent_uses_packaged(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path))
    template = load_template("review")
    assert template is not None and "OPERATOR OVERRIDE" not in template


# --------------------------------------------------------------------------- #
# role field validation
# --------------------------------------------------------------------------- #
def test_role_field_normalized_lowercase():
    spec = validate_job_spec({
        "repo": {"path": "/tmp/x"},
        "request": {"type": "task", "title": "t", "body": "b"},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
        "role": "Review",
    })
    assert spec["role"] == "review"


def test_role_field_defaults_empty():
    spec = validate_job_spec({
        "repo": {"path": "/tmp/x"},
        "request": {"type": "task", "title": "t", "body": "b"},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
    })
    assert spec["role"] == ""


def test_role_field_rejects_unknown():
    with pytest.raises(ValueError, match="role"):
        validate_job_spec({
            "repo": {"path": "/tmp/x"},
            "request": {"type": "task", "title": "t", "body": "b"},
            "branch": {"mode": "detached-review"},
            "delivery_mode": "report-only",
            "role": "architect",
        })


# --------------------------------------------------------------------------- #
# job_prompt rendering
# --------------------------------------------------------------------------- #
def test_job_prompt_review_role_has_adversarial_mission_and_json_contract():
    prompt = job_prompt(_role_record("review"))
    # adversarial stance
    assert "you did not write this code" in prompt.lower()
    assert "until proven otherwise" in prompt.lower()
    assert "do not trust" in prompt.lower()
    # JSON findings contract — rendered (braces un-escaped) and verdict line
    assert '"severity":"critical|high|medium|low"' in prompt
    assert '"suggestion"' in prompt
    assert "VERDICT:" in prompt
    # operational sections are kept
    assert "Scope contract:" in prompt
    assert "Operational rules:" in prompt
    # placeholders are all filled (no leftover braces from our field set)
    for placeholder in ("{job_id}", "{body}", "{branch_contract}", "{scope_contract}",
                        "{delivery_contract}", "{upstream_context}"):
        assert placeholder not in prompt


def test_job_prompt_plan_role_allows_reject_or_split():
    prompt = job_prompt(_role_record("plan"))
    assert "REJECT" in prompt and "SPLIT" in prompt
    assert "test strategy" in prompt.lower()


def test_job_prompt_implement_role_has_tdd_contract():
    prompt = job_prompt(_role_record("implement"))
    assert "failing test first" in prompt.lower()
    assert "never weaken a test" in prompt.lower()


def test_job_prompt_audit_role_has_owasp_decomposition_and_rules():
    prompt = job_prompt(_role_record("audit"))
    # OWASP Top 10 spine + survey-first instruction
    assert "OWASP Top 10" in prompt
    assert "survey" in prompt.lower() and "attack surface" in prompt.lower()
    # decomposition: proposed_tasks contract with its scoped-child fields
    assert "proposed_tasks" in prompt
    assert "allowed_paths" in prompt
    assert "severity_rationale" in prompt
    # same JSON findings contract as the review role (rendered, braces un-escaped)
    assert '"severity":"critical|high|medium|low"' in prompt
    assert '"suggestion"' in prompt
    assert "VERDICT:" in prompt
    # hard rules: never exploit / never exfiltrate / never modify, secrets = CRITICAL
    lower = prompt.lower()
    assert "never exploit" in lower
    assert "never exfiltrate" in lower
    assert "must not modify" in lower
    assert "hardcoded secret" in lower and "rotat" in lower
    # placeholders are all filled (no leftover braces from our field set)
    for placeholder in ("{job_id}", "{body}", "{branch_contract}", "{scope_contract}",
                        "{delivery_contract}", "{upstream_context}"):
        assert placeholder not in prompt


def test_job_prompt_without_role_matches_snapshot():
    assert job_prompt(_snapshot_record()) == _GENERIC_SNAPSHOT


def test_job_prompt_unknown_role_falls_back_to_generic():
    # An unknown role cannot reach normalize/validate, but a record carrying one
    # directly (e.g. legacy data) must still produce the generic prompt.
    rec = _snapshot_record()
    rec["spec"]["role"] = "architect"  # no template for this role
    assert job_prompt(rec) == _GENERIC_SNAPSHOT
