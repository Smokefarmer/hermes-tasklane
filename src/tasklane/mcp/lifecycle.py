"""TaskLane MCP lifecycle tools (registered on the shared FastMCP instance)."""

from __future__ import annotations

from tasklane.mcp.core import *  # noqa: F401,F403 — shared hub (mcp, audited, helpers)
from tasklane.mcp.core import (  # explicit for linters / clarity
    mcp, audited, _cfg, _store, _get_record, _worktree_dir, _readable_dir,
    _safe_path, _run, _summary, _live_worktree, _audit, logger, repo_path_allowed,
    pairing, cleanup_worktree, worktrees_root, _WORKER_SERVICE, _MAX_READ_BYTES, _MAX_OUT,
)


# --------------------------------------------------------------------------- #
# lifecycle tools
# --------------------------------------------------------------------------- #
@mcp.tool()
@audited
def create_task(
    repo: str,
    title: str,
    body: str,
    type: str = "task-small",
    branch_mode: str = "new-branch",
    base_branch: str | None = None,
    work_branch: str | None = None,
    pr_target: str | None = None,
    delivery_mode: str = "pull-request",
    id: str | None = None,
) -> dict[str, Any]:
    """Create a coding job. repo must be an absolute path to a git repo (and within the
    configured allowlist). branch_mode: new-branch|existing-branch|detached-review.
    delivery_mode: pull-request|direct-push|report-only. Returns the new job id+state."""
    cfg = _cfg()
    if not repo_path_allowed(repo, cfg):
        raise ValueError(f"repo path not in allowlist: {repo}")
    spec: dict[str, Any] = {
        "repo": {"path": repo},
        "request": {"type": type, "title": title, "body": body},
        "delivery_mode": delivery_mode,
        "branch": {"mode": branch_mode, "base_branch": base_branch, "work_branch": work_branch, "pr_target": pr_target},
    }
    if id:
        spec["id"] = id
    record = _store().put(spec)
    return {"id": record["id"], "state": record["state"]}


_PIPELINE_STAGES = ("plan", "implement", "review", "test")


def _pipeline_stage_specs(base_id: str, repo: str, title: str, body: str, *, stages: list[str],
                          work_branch: str, base_branch: str, pr_target: str | None,
                          delivery_mode: str) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    prior_stage_id: str | None = None
    for stage in stages:
        stage_id = f"{base_id}-{stage}"
        if stage == "plan":
            spec: dict[str, Any] = {
                "id": stage_id,
                "repo": {"path": repo},
                "request": {"type": "task", "title": f"[plan] {title}",
                            "body": ("Planning stage of a TaskLane pipeline. Produce a concrete, "
                                     "step-by-step implementation plan for the task below: files to "
                                     "touch, approach, risks, and test strategy. Do NOT modify any "
                                     f"files.\n\nTask:\n{body}")},
                "branch": {"mode": "detached-review", "base_branch": base_branch},
                "delivery_mode": "report-only",
            }
        elif stage == "implement":
            spec = {
                "id": stage_id,
                "repo": {"path": repo},
                "request": {"type": "task", "title": f"[implement] {title}", "body": body},
                "branch": {"mode": "new-branch", "base_branch": base_branch,
                           "work_branch": work_branch, "pr_target": pr_target},
                "delivery_mode": delivery_mode,
            }
        elif stage == "review":
            spec = {
                "id": stage_id,
                "repo": {"path": repo},
                "request": {"type": "task", "title": f"[review] {title}",
                            "body": (f"Review stage of a TaskLane pipeline. Review the changes on "
                                     f"branch {work_branch} relative to {base_branch} "
                                     f"(git diff {base_branch}...HEAD). Check correctness, security, "
                                     "and test coverage. Report findings with severity; do NOT "
                                     f"modify any files.\n\nOriginal task:\n{body}")},
                "branch": {"mode": "detached-review", "base_branch": work_branch},
                "delivery_mode": "report-only",
            }
        else:  # test — verify the work branch by running it (report-only)
            spec = {
                "id": stage_id,
                "repo": {"path": repo},
                "request": {"type": "task-small", "title": f"[test] {title}",
                            "body": (f"Test stage of a TaskLane pipeline. The change is on branch "
                                     f"{work_branch}. Verify it by ACTUALLY RUNNING it per your tester "
                                     "role (boot the app / exercise the changed behaviour end-to-end), "
                                     "using the project's test commands and the credentials named in "
                                     f"your prompt. Report PASS/FAIL with evidence; modify nothing.\n\n"
                                     f"Original task:\n{body}")},
                "branch": {"mode": "detached-review", "base_branch": work_branch},
                "delivery_mode": "report-only",
            }
        # Each stage selects its role template; the test stage uses the test-local tester role.
        spec["role"] = "test-local" if stage == "test" else stage
        if prior_stage_id:
            spec["dependencies"] = [prior_stage_id]
            spec["context_from"] = [prior_stage_id]
        specs.append(spec)
        prior_stage_id = stage_id
    return specs


@mcp.tool()
@audited
def create_pipeline(
    repo: str,
    title: str,
    body: str,
    work_branch: str,
    base_branch: str = "main",
    pr_target: str | None = None,
    delivery_mode: str = "pull-request",
    id: str | None = None,
    stages: str = "plan,implement,review",
) -> dict[str, Any]:
    """Create a multi-stage assembly-line pipeline as dependency-chained jobs:
    plan (report-only) -> implement (delivers on work_branch) -> review (report-only
    on the work branch) -> test (optional; report-only tester role that RUNS the work
    branch). Each stage receives the previous stage's final response as context.
    stages: comma-separated subset of plan,implement,review,test (implement required;
    default 'plan,implement,review'). Returns the created job ids in execution order."""
    cfg = _cfg()
    if not repo_path_allowed(repo, cfg):
        raise ValueError(f"repo path not in allowlist: {repo}")
    wanted = [s.strip().lower() for s in stages.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in _PIPELINE_STAGES]
    if unknown:
        raise ValueError(f"unknown stages: {', '.join(unknown)} (allowed: {', '.join(_PIPELINE_STAGES)})")
    ordered = [s for s in _PIPELINE_STAGES if s in wanted]
    if "implement" not in ordered:
        raise ValueError("the implement stage is required")
    if delivery_mode == "pull-request" and not pr_target:
        pr_target = base_branch
    base_id = (id or "").strip() or re.sub(r"[^a-z0-9_.-]+", "-", title.lower()).strip("-._") or "pipeline"
    specs = _pipeline_stage_specs(base_id, repo, title, body, stages=ordered,
                                  work_branch=work_branch, base_branch=base_branch,
                                  pr_target=pr_target, delivery_mode=delivery_mode)
    store = _store()
    clashes = [s["id"] for s in specs if store.get(s["id"]) is not None]
    if clashes:
        raise ValueError(f"job ids already exist: {', '.join(clashes)}")
    created = [store.put(spec) for spec in specs]
    return {"pipeline": base_id, "jobs": [{"id": r["id"], "state": r["state"]} for r in created]}


def _analyze_job_spec(repo: str, *, base_branch: str, id: str | None) -> dict[str, Any]:
    """Build the spec for an architecture-audit job.

    One job, role ``analyze``, on a fresh review branch delivered direct-push so
    the written review document arrives as a reviewable branch. request.type is
    ``refactor-large`` (a whole-repo audit, not a small task)."""
    job_id = (id or "").strip() or None
    work_branch = f"tasklane/architecture-review-{job_id}" if job_id else "tasklane/architecture-review"
    spec: dict[str, Any] = {
        "repo": {"path": repo},
        "request": {
            "type": "refactor-large",
            "title": "Architecture review",
            "body": (
                "Audit this entire repository as an architecture reviewer. Run the four "
                "passes (Map, Intent, Patterns, Accretion hotspots) defined in your role "
                "prompt, write docs/architecture-review.md with ADR drafts under "
                "docs/adr/proposed/, and end with a proposed_tasks block of ranked "
                "remediation tasks."
            ),
        },
        "role": "analyze",
        "branch": {"mode": "new-branch", "base_branch": base_branch, "work_branch": work_branch},
        "delivery_mode": "direct-push",
    }
    if job_id:
        spec["id"] = job_id
    return spec


@mcp.tool()
@audited
def analyze_project(repo: str, base_branch: str = "main", id: str | None = None) -> dict[str, Any]:
    """Audit a whole repository's architecture against best practices and its own
    documented intent. Creates one job with the analyze role on a fresh review
    branch (tasklane/architecture-review[-id]), delivered direct-push, so the
    written review (docs/architecture-review.md + ADR drafts) lands as a
    reviewable branch. The review ends with a proposed_tasks block whose
    remediation items land as drafts requiring approval. repo must be an absolute
    path to a git repo within the configured allowlist. Returns the job id+state."""
    cfg = _cfg()
    if not repo_path_allowed(repo, cfg):
        raise ValueError(f"repo path not in allowlist: {repo}")
    record = _store().put(_analyze_job_spec(repo, base_branch=base_branch, id=id))
    return {"id": record["id"], "state": record["state"]}


@mcp.tool()
@audited
def security_audit(
    repo: str,
    base_branch: str = "main",
    scope: str | None = None,
    id: str | None = None,
) -> dict[str, Any]:
    """Create ONE report-only security audit job (role 'audit', detached-review).

    The audit agent surveys the attack surface against the OWASP Top 10, then either
    reports findings directly (small codebase) or emits a `proposed_tasks` block of
    scoped, report-only child audits an operator can approve. It never modifies code.
    repo must be an absolute path within the configured allowlist. scope optionally
    narrows the survey (e.g. "backend auth only"). Returns the new job id+state."""
    cfg = _cfg()
    if not repo_path_allowed(repo, cfg):
        raise ValueError(f"repo path not in allowlist: {repo}")
    focus = (scope or "").strip()
    title = f"Security audit: {focus}" if focus else "Security audit"
    body = (
        "Perform a security audit of this repository. Survey the attack surface "
        "(entry points, trust boundaries, authn/authz, data stores, third-party "
        "calls, secrets handling) using the OWASP Top 10 as the checklist spine. "
        "Then EITHER report findings directly if the codebase is small enough to "
        "read exhaustively, OR propose focused, report-only child audits via a "
        "proposed_tasks block. Never exploit, never exfiltrate, never modify code."
    )
    if focus:
        body += f"\n\nScope / focus for this audit: {focus}"
    spec: dict[str, Any] = {
        "repo": {"path": repo},
        "request": {"type": "task-small", "title": title, "body": body},
        "branch": {"mode": "detached-review", "base_branch": base_branch},
        "delivery_mode": "report-only",
        "role": "audit",
    }
    if id:
        spec["id"] = id
    record = _store().put(spec)
    return {"id": record["id"], "state": record["state"], "role": "audit"}


@mcp.tool()
@audited
def list_tasks(state: str | None = None) -> list[dict[str, Any]]:
    """List jobs, optionally filtered by state (ready|running|blocked|completed|failed|needs-human|draft)."""
    states = [state] if state else None
    return [_summary(r) for r in _store().list(states=states)]


@mcp.tool()
@audited
def get_task(job_id: str) -> dict[str, Any]:
    """Return the full job record (spec, state, attempt, result, last_error, worktree)."""
    return _get_record(job_id)


@mcp.tool()
@audited
def task_events(job_id: str, limit: int | None = 50) -> list[dict[str, Any]]:
    """Return the job's event timeline (most recent `limit`)."""
    return _store().events(job_id, limit=limit)


@mcp.tool()
@audited
def task_logs(job_id: str, tail: int = 200) -> dict[str, Any]:
    """Return the tail of the job's captured claude -p log."""
    path = logs_root() / f"{job_id}.log"
    if not path.exists():
        return {"job_id": job_id, "log": "", "note": "no log yet"}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"job_id": job_id, "log": "\n".join(lines[-max(1, tail):])}


@mcp.tool()
@audited
def retry_task(job_id: str, extra_instructions: str | None = None) -> dict[str, Any]:
    """Re-queue a blocked/failed job (-> ready). If extra_instructions is given, it is
    appended to the task body so the next run sees your guidance."""
    store = _store()
    record = _get_record(job_id)
    if extra_instructions:
        spec = dict(record.get("spec") or {})
        request = dict(spec.get("request") or {})
        request["body"] = f"{request.get('body', '')}\n\n[Operator follow-up]\n{extra_instructions}"
        spec["request"] = request
        store.transition(job_id, record["state"], reason="operator-instructions-added", updates={"spec": spec})
    updated = store.retry(job_id)
    return {"id": job_id, "state": updated["state"]}


@mcp.tool()
@audited
def cancel_task(job_id: str) -> dict[str, Any]:
    """Cancel a non-terminal job (-> failed)."""
    updated = _store().cancel(job_id)
    return {"id": job_id, "state": updated["state"]}


@mcp.tool()
@audited
def run_task_now(job_id: str) -> dict[str, Any]:
    """Nudge a job to run: blocked/failed -> ready (the worker claims ready jobs every few
    seconds). Ready/running jobs are reported as already queued."""
    record = _get_record(job_id)
    state = record.get("state")
    if state in {"blocked", "failed", "needs-human"}:
        updated = _store().retry(job_id)
        return {"id": job_id, "state": updated["state"], "note": "requeued"}
    return {"id": job_id, "state": state, "note": "already queued or running"}

