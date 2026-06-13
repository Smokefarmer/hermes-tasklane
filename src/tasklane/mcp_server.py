"""TaskLane MCP control plane (Streamable HTTP).

Exposes the job lifecycle plus full worktree fix-tools to a remote Claude (e.g.
the user's laptop) over MCP. Bound to localhost; fronted by the Cloudflare tunnel
+ Cloudflare Access. Two independent gates protect it: Cloudflare Access (service
token) and an app bearer token checked here. Every tool call is audit-logged.
"""

from __future__ import annotations

import functools
import hmac
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from tasklane import pairing
from tasklane.config import Config, load_config, repo_path_allowed
from tasklane.paths import audit_log_path, logs_root, worktrees_root
from tasklane.store import JobStore
from tasklane.worktree import cleanup_worktree

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("tasklane.mcp")

_WORKER_SERVICE = "tasklane-worker.service"
_MAX_READ_BYTES = 200_000
_MAX_OUT = 20_000
_MAX_PAIR_BODY_BYTES = 4_096
_MAX_CLIENT_NAME_LENGTH = 120

mcp = FastMCP("tasklane")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _cfg() -> Config:
    return load_config()


def _store() -> JobStore:
    s = JobStore()
    s.ensure_dirs()
    return s


def _audit(tool: str, info: dict[str, Any], status: str) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "tool": tool, "status": status, "info": info}
    try:
        with audit_log_path().open("a", encoding="utf-8") as h:
            h.write(json.dumps(entry, default=str) + "\n")
    except Exception:  # noqa: BLE001 — auditing must never break a tool
        logger.exception("audit write failed")


def audited(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        status = "ok"
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            status = f"error:{type(exc).__name__}"
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            _audit(fn.__name__, dict(kwargs), status)
    return wrapper


def _get_record(job_id: str) -> dict[str, Any]:
    record = _store().get(job_id)
    if record is None:
        raise FileNotFoundError(f"job not found: {job_id}")
    return record


def _worktree_dir(record: dict[str, Any]) -> Path:
    wt = (record.get("worktree") or {}).get("worktree_path")
    if wt and Path(wt).is_dir():
        return Path(wt)
    repo = ((record.get("spec") or {}).get("repo") or {}).get("path")
    if repo and Path(repo).expanduser().is_dir():
        return Path(repo).expanduser()
    raise FileNotFoundError(f"no worktree available for job {record.get('id')} (state={record.get('state')})")


def _safe_path(base: Path, rel: str) -> Path:
    base = base.resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"path escapes worktree: {rel}")
    return target


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> dict[str, Any]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"returncode": 124, "stdout": "", "stderr": f"timed out after {timeout}s"}
    return {"returncode": p.returncode, "stdout": (p.stdout or "")[-_MAX_OUT:], "stderr": (p.stderr or "")[-_MAX_OUT:]}


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    spec = record.get("spec") or {}
    job_metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
    return {
        "id": record.get("id"),
        "state": record.get("state"),
        "attempt": record.get("attempt"),
        "title": (spec.get("request") or {}).get("title"),
        "repo": (spec.get("repo") or {}).get("path"),
        "last_error": record.get("last_error"),
        "updated_at": record.get("updated_at"),
        "cost_usd": job_metrics.get("cost_usd"),
    }


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
# Stage name -> role template. The optional "test" stage uses the local TESTER role.
_STAGE_ROLES = {"plan": "plan", "implement": "implement", "review": "review", "test": "test-local"}


def _pipeline_stage_specs(base_id: str, repo: str, title: str, body: str, *, stages: list[str],
                          work_branch: str, base_branch: str, pr_target: str | None,
                          delivery_mode: str, project: str | None = None) -> list[dict[str, Any]]:
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
        else:  # test (local TESTER role): verify the implementation by running it
            spec = {
                "id": stage_id,
                "repo": {"path": repo},
                "request": {"type": "task-small", "title": f"[test] {title}",
                            "body": (f"Test stage of a TaskLane pipeline. Verify the implementation on "
                                     f"branch {work_branch} by actually RUNNING it (boot the app, exercise "
                                     "the changed behaviour end-to-end). Report findings with severity and "
                                     f"a PASS/FAIL verdict; do NOT modify any files.\n\nOriginal task:\n{body}")},
                "branch": {"mode": "detached-review", "base_branch": work_branch},
                "delivery_mode": "report-only",
            }
        # Each pipeline stage selects its role-based prompt template.
        spec["role"] = _STAGE_ROLES[stage]
        # The test stage needs the project profile to inject test credentials; other
        # stages do not (and should not receive secrets they have no use for).
        if stage == "test" and project:
            spec["project"] = project
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
    project: str | None = None,
) -> dict[str, Any]:
    """Create a multi-stage assembly-line pipeline as dependency-chained jobs:
    plan (report-only) -> implement (delivers on work_branch) -> review (report-only
    on the work branch) -> test (optional, report-only; verifies by running it). Each
    stage receives the previous stage's final response as context. stages:
    comma-separated subset of plan,implement,review,test (implement required); the
    default omits test — pass stages="plan,implement,review,test" to opt in. project
    (if set) selects the config project profile whose secret env file the test stage
    uses for credentials. Returns the created job ids in execution order."""
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
                                  pr_target=pr_target, delivery_mode=delivery_mode,
                                  project=project)
    store = _store()
    clashes = [s["id"] for s in specs if store.get(s["id"]) is not None]
    if clashes:
        raise ValueError(f"job ids already exist: {', '.join(clashes)}")
    created = [store.put(spec) for spec in specs]
    return {"pipeline": base_id, "jobs": [{"id": r["id"], "state": r["state"]} for r in created]}


_TEST_MODE_ROLES = {"staging": "test-staging", "local": "test-local"}


@mcp.tool()
@audited
def test_deployment(
    repo: str,
    mode: str = "staging",
    flows: str = "",
    project: str | None = None,
    id: str | None = None,
) -> dict[str, Any]:
    """Create a report-only TESTER job that verifies a deployment by running it.

    mode: "staging" drives the deployed frontend with Playwright (test-staging role);
    "local" boots the app in the worktree and exercises it (test-local role). flows is
    free text listing the user flows to verify; it goes into the job body. project (if
    given) selects the config project profile that provides the secret env file and
    test commands. The job is detached-review + report-only (no commits/pushes)."""
    cfg = _cfg()
    if not repo_path_allowed(repo, cfg):
        raise ValueError(f"repo path not in allowlist: {repo}")
    normalized_mode = (mode or "staging").strip().lower()
    role = _TEST_MODE_ROLES.get(normalized_mode)
    if role is None:
        raise ValueError(f"unknown mode: {mode} (allowed: {', '.join(sorted(_TEST_MODE_ROLES))})")

    flows_text = (flows or "").strip()
    where = "the deployed staging environment" if normalized_mode == "staging" else "a locally-booted instance"
    body = (
        f"Deployment verification job ({normalized_mode}). Verify the system against "
        f"{where} by actually running it, per your TESTER role instructions.\n\n"
        "User flows to verify:\n"
        + (flows_text or "(none specified — exercise the core changed behaviour end-to-end)")
    )
    spec: dict[str, Any] = {
        "repo": {"path": repo},
        "request": {"type": "task-small", "title": f"[test-{normalized_mode}] verify deployment", "body": body},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
        "role": role,
    }
    if project:
        spec["project"] = project
    if id:
        spec["id"] = id
    record = _store().put(spec)
    return {"id": record["id"], "state": record["state"], "role": role, "mode": normalized_mode}


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


# --------------------------------------------------------------------------- #
# inspect & fix tools (confined to the job's worktree)
# --------------------------------------------------------------------------- #
@mcp.tool()
@audited
def get_diff(job_id: str) -> dict[str, Any]:
    """Show the job worktree's git state: status, unstaged diff, and staged diff."""
    wt = _worktree_dir(_get_record(job_id))
    return {
        "worktree": str(wt),
        "status": _run(["git", "status", "--porcelain", "--branch"], cwd=wt, timeout=30),
        "diff": _run(["git", "diff"], cwd=wt, timeout=30),
        "staged_diff": _run(["git", "diff", "--cached"], cwd=wt, timeout=30),
    }


@mcp.tool()
@audited
def list_dir(job_id: str, path: str = ".") -> dict[str, Any]:
    """List a directory inside the job worktree."""
    wt = _worktree_dir(_get_record(job_id))
    target = _safe_path(wt, path)
    if not target.is_dir():
        raise NotADirectoryError(f"not a directory: {path}")
    entries = []
    for child in sorted(target.iterdir()):
        entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file",
                        "size": child.stat().st_size if child.is_file() else None})
    return {"path": str(target.relative_to(wt.resolve())), "entries": entries}


@mcp.tool()
@audited
def read_file(job_id: str, path: str) -> dict[str, Any]:
    """Read a file inside the job worktree (truncated to 200KB)."""
    wt = _worktree_dir(_get_record(job_id))
    target = _safe_path(wt, path)
    if not target.is_file():
        raise FileNotFoundError(f"not a file: {path}")
    data = target.read_bytes()[:_MAX_READ_BYTES]
    return {"path": path, "truncated": target.stat().st_size > _MAX_READ_BYTES,
            "content": data.decode("utf-8", errors="replace")}


@mcp.tool()
@audited
def write_file(job_id: str, path: str, content: str) -> dict[str, Any]:
    """Write (create/overwrite) a file inside the job worktree."""
    wt = _worktree_dir(_get_record(job_id))
    target = _safe_path(wt, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": path, "bytes_written": len(content.encode("utf-8"))}


@mcp.tool()
@audited
def apply_patch(job_id: str, unified_diff: str) -> dict[str, Any]:
    """Apply a unified diff to the job worktree via `git apply`."""
    wt = _worktree_dir(_get_record(job_id))
    patch_file = wt / ".tasklane-patch.diff"
    patch_file.write_text(unified_diff, encoding="utf-8")
    try:
        return _run(["git", "apply", "--whitespace=nowarn", str(patch_file)], cwd=wt, timeout=60)
    finally:
        patch_file.unlink(missing_ok=True)


@mcp.tool()
@audited
def exec(job_id: str, command: str, timeout: int | None = None) -> dict[str, Any]:
    """Run a shell command inside the job worktree (bash -lc). Full power within the worktree."""
    wt = _worktree_dir(_get_record(job_id))
    t = int(timeout or _cfg().exec_timeout_seconds)
    return _run(["bash", "-lc", command], cwd=wt, timeout=t)


@mcp.tool()
@audited
def git(job_id: str, args: str) -> dict[str, Any]:
    """Run a git command inside the job worktree, e.g. args='status --short' or 'commit -am fix'."""
    wt = _worktree_dir(_get_record(job_id))
    return _run(["bash", "-lc", f"git {args}"], cwd=wt, timeout=120)


@mcp.tool()
@audited
def run_tests(job_id: str, command: str | None = None) -> dict[str, Any]:
    """Run a test command inside the job worktree. Defaults to auto-detect (pytest / npm test)."""
    wt = _worktree_dir(_get_record(job_id))
    cmd = command
    if not cmd:
        if (wt / "package.json").exists():
            cmd = "npm test"
        elif any((wt / p).exists() for p in ("pytest.ini", "pyproject.toml", "tests", "setup.cfg")):
            cmd = "pytest -q"
        else:
            return {"error": "could not auto-detect a test command; pass command="}
    return _run(["bash", "-lc", cmd], cwd=wt, timeout=_cfg().exec_timeout_seconds)


# --------------------------------------------------------------------------- #
# client pairing (connection approval)
# --------------------------------------------------------------------------- #
@mcp.tool()
@audited
def pairing_requests() -> list[dict[str, Any]]:
    """List unexpired pending pairing requests (pairing_code, client name, requested_at)."""
    return pairing.pending_requests()


@mcp.tool()
@audited
def approve_client(pairing_code: str) -> dict[str, Any]:
    """Approve a pending pairing request; the client's token starts authenticating immediately."""
    return pairing.approve_client(pairing_code)


@mcp.tool()
@audited
def reject_client(pairing_code: str) -> dict[str, Any]:
    """Reject a pending pairing request and discard its client record."""
    return pairing.reject_client(pairing_code)


@mcp.tool()
@audited
def list_clients() -> list[dict[str, Any]]:
    """List all paired clients (pending, approved, revoked); token hashes are never returned."""
    return pairing.list_clients()


@mcp.tool()
@audited
def revoke_client(client_id: str) -> dict[str, Any]:
    """Revoke a paired client; its token stops authenticating immediately."""
    return pairing.revoke_client(client_id)


# --------------------------------------------------------------------------- #
# worker / system ops (guardrailed)
# --------------------------------------------------------------------------- #
@mcp.tool()
@audited
def worker_status() -> dict[str, Any]:
    """Report worker service health and queue depth by state."""
    store = _store()
    counts: dict[str, int] = {}
    for r in store.list():
        counts[r.get("state", "?")] = counts.get(r.get("state", "?"), 0) + 1
    active = _run(["systemctl", "is-active", _WORKER_SERVICE], cwd=Path("/"), timeout=10)["stdout"].strip()
    return {"worker_service": _WORKER_SERVICE, "active": active, "counts": counts}


@mcp.tool()
@audited
def restart_worker() -> dict[str, Any]:
    """Restart the TaskLane worker service (requires the scoped sudoers entry)."""
    return _run(["sudo", "-n", "systemctl", "restart", _WORKER_SERVICE], cwd=Path("/"), timeout=30)


@mcp.tool()
@audited
def prune_worktrees() -> dict[str, Any]:
    """Remove worktrees of completed jobs and prune dangling git worktree refs."""
    store = _store()
    removed = []
    for record in store.list(states=["completed", "failed"]):
        wt = (record.get("worktree") or {})
        if wt.get("worktree_path") and Path(wt["worktree_path"]).exists():
            cleanup_worktree(wt, keep=False)
            removed.append(wt["worktree_path"])
    pruned = []
    for repo_dir in (worktrees_root().glob("*") if worktrees_root().exists() else []):
        pruned.append(repo_dir.name)
    return {"removed_worktrees": removed, "repos_seen": pruned}


@mcp.tool()
@audited
def metrics() -> dict[str, Any]:
    """Store-wide telemetry: job counts by state, total/24h cost (USD), token usage,
    average wall time. Per-job metrics live on each record (get_task)."""
    from tasklane.metrics import aggregate

    return aggregate(_store())


@mcp.tool()
@audited
def reconcile_jobs() -> dict[str, Any]:
    """Recover orphaned running jobs (dead worker process) and remove stale claim
    locks. Safe to run anytime; the worker also runs this periodically."""
    from tasklane.reconcile import reconcile

    return reconcile(_store(), _cfg())


@mcp.tool()
@audited
def admin_exec(command: str, timeout: int | None = None) -> dict[str, Any]:
    """Run a server-level shell command (NOT confined to a worktree). Disabled unless
    enable_admin_exec is true in config. Use for broader server fixes."""
    cfg = _cfg()
    if not cfg.enable_admin_exec:
        raise PermissionError("admin_exec is disabled; set enable_admin_exec: true in config to allow")
    t = int(timeout or cfg.exec_timeout_seconds)
    return _run(["bash", "-lc", command], cwd=Path(os.path.expanduser("~")), timeout=t)


# --------------------------------------------------------------------------- #
# browser status page
# --------------------------------------------------------------------------- #
def _status_data() -> dict[str, Any]:
    from tasklane.metrics import spend_last_24h

    store = _store()
    rows = store.list()
    counts: dict[str, int] = {}
    total_cost = 0.0
    for r in rows:
        counts[r.get("state", "?")] = counts.get(r.get("state", "?"), 0) + 1
        m = r.get("metrics")
        if isinstance(m, dict):
            try:
                total_cost += float(m.get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass
    recent = sorted(rows, key=lambda r: r.get("updated_at", ""), reverse=True)[:15]
    active = _run(["systemctl", "is-active", _WORKER_SERVICE], cwd=Path("/"), timeout=10)["stdout"].strip()
    return {"worker_active": active, "counts": counts, "recent": [_summary(r) for r in recent],
            "spend_24h_usd": spend_last_24h(store), "spend_total_usd": round(total_cost, 4)}


def _status_html(d: dict[str, Any]) -> str:
    badge = {"completed": "#2e7d32", "running": "#1565c0", "blocked": "#ef6c00",
             "failed": "#c62828", "ready": "#6a1b9a", "needs-human": "#ad1457"}
    rows = "".join(
        f"<tr><td><code>{j['id']}</code></td>"
        f"<td><span style='color:{badge.get(j['state'],'#555')};font-weight:600'>{j['state']}</span></td>"
        f"<td>{j.get('attempt',0)}</td><td>{(j.get('title') or '')[:60]}</td>"
        f"<td>{('$%.2f' % j['cost_usd']) if j.get('cost_usd') else ''}</td>"
        f"<td style='color:#a00'>{(j.get('last_error') or '')[:60]}</td>"
        f"<td style='color:#888'>{(j.get('updated_at') or '')[:19]}</td></tr>"
        for j in d["recent"]
    ) or "<tr><td colspan=7 style='color:#888'>no jobs</td></tr>"
    counts = " · ".join(f"{k}: <b>{v}</b>" for k, v in sorted(d["counts"].items())) or "no jobs"
    spend = f"24h: <b>${d.get('spend_24h_usd', 0):.2f}</b> / total: <b>${d.get('spend_total_usd', 0):.2f}</b>"
    wa = d["worker_active"]
    wc = "#2e7d32" if wa == "active" else "#c62828"
    return f"""<!doctype html><html><head><meta charset=utf-8><title>TaskLane status</title>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=15>
<style>body{{font-family:system-ui,Arial;margin:24px;color:#222}}table{{border-collapse:collapse;width:100%;margin-top:12px}}
td,th{{border-bottom:1px solid #eee;padding:6px 10px;text-align:left;font-size:14px}}th{{color:#666}}code{{font-size:13px}}</style></head>
<body><h2>TaskLane</h2>
<p>worker: <b style="color:{wc}">{wa}</b> &nbsp;|&nbsp; {counts} &nbsp;|&nbsp; {spend} &nbsp;|&nbsp; <span style=color:#888>auto-refresh 15s</span></p>
<table><tr><th>job</th><th>state</th><th>try</th><th>title</th><th>cost</th><th>last error</th><th>updated (UTC)</th></tr>{rows}</table>
</body></html>"""


# --------------------------------------------------------------------------- #
# ASGI auth middleware + entrypoint
# --------------------------------------------------------------------------- #
async def _read_body(receive) -> bytes:
    """Drain an ASGI request body, stopping just past the /pair size cap."""
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > _MAX_PAIR_BODY_BYTES or not message.get("more_body"):
            return body


def _parse_pair_name(body: bytes) -> str | None:
    """Extract a valid client name from a /pair JSON body, or None if invalid."""
    if len(body) > _MAX_PAIR_BODY_BYTES:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or len(name) > _MAX_CLIENT_NAME_LENGTH:
        return None
    return name


class AuthMiddleware:
    """Pure-ASGI gate: app bearer token or approved paired-client token,
    plus Origin validation (anti DNS-rebind)."""

    def __init__(self, app, *, token: str, allowed_origins: set[str], pairing_enabled: bool = False):
        self.app = app
        self.token = token
        self.allowed_origins = allowed_origins
        self.pairing_enabled = pairing_enabled

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        if path == "/health":
            return await self._json(send, 200, {"status": "ok"})
        client_ip = headers.get("cf-connecting-ip") or (headers.get("x-forwarded-for", "").split(",")[0].strip()) or "?"
        if path == "/pair":
            return await self._pair(scope, receive, send, client_ip)
        if path == "/status":
            # browser-friendly: token via ?token= or Authorization header
            from urllib.parse import parse_qs
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            qtok = (qs.get("token") or [""])[0]
            auth = headers.get("authorization", "")
            htok = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            supplied = qtok or htok
            if not self.token or not hmac.compare_digest(supplied, self.token):
                _audit("status", {"ip": client_ip, "reason": "bad-token"}, "rejected")
                return await self._html(send, 401, "<h3>401 — append ?token=&lt;app_token&gt;</h3>")
            try:
                return await self._html(send, 200, _status_html(_status_data()))
            except Exception as exc:  # noqa: BLE001
                return await self._html(send, 500, f"<pre>status error: {exc}</pre>")
        origin = headers.get("origin")
        if origin and origin not in self.allowed_origins:
            _audit("auth", {"ip": client_ip, "path": path, "reason": "origin"}, "rejected")
            return await self._json(send, 403, {"error": "origin not allowed"})
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if not self._authorized(token):
            _audit("auth", {"ip": client_ip, "path": path, "reason": "bad-token"}, "rejected")
            return await self._json(send, 401, {"error": "invalid or missing bearer token"})
        return await self.app(scope, receive, send)

    def _authorized(self, token: str) -> bool:
        """True for the legacy app token or an approved, non-revoked paired client."""
        if self.token and hmac.compare_digest(token, self.token):
            return True
        if not self.pairing_enabled or not token:
            return False
        return pairing.authenticate(token) is not None

    async def _pair(self, scope, receive, send, client_ip: str) -> None:
        """Public pairing endpoint: POST {"name": ...} -> pending client + one-time token."""
        if not self.pairing_enabled:
            _audit("pair", {"ip": client_ip, "reason": "disabled"}, "rejected")
            return await self._json(send, 404, {"error": "pairing is disabled"})
        if scope.get("method") != "POST":
            _audit("pair", {"ip": client_ip, "reason": "method"}, "rejected")
            return await self._json(send, 405, {"error": "use POST"})
        name = _parse_pair_name(await _read_body(receive))
        if name is None:
            _audit("pair", {"ip": client_ip, "reason": "bad-body"}, "rejected")
            return await self._json(send, 400, {"error": 'expected JSON body {"name": "<client name>"}'})
        try:
            result = pairing.request_pairing(name)
        except ValueError as exc:  # pending cap reached
            _audit("pair", {"ip": client_ip, "name": name, "reason": "pending-cap"}, "rejected")
            return await self._json(send, 429, {"error": str(exc)})
        _audit("pair", {"ip": client_ip, "name": name, "client_id": result["client_id"],
                        "pairing_code": result["pairing_code"]}, "ok")
        return await self._json(send, 200, result)

    @staticmethod
    async def _json(send, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _html(send, status: int, html: str) -> None:
        body = html.encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def build_app(cfg: Config):
    from mcp.server.transport_security import TransportSecuritySettings

    if not cfg.app_token:
        raise RuntimeError("config.app_token is empty; refusing to start without an app token")
    mcp.settings.host = cfg.mcp_host
    mcp.settings.port = cfg.mcp_port
    # DNS-rebinding protection: allow localhost plus the configured public hostnames
    # (the Host header arriving via the Cloudflare tunnel, e.g. tasklane.example.com).
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    for h in cfg.public_hostnames:
        allowed_hosts += [h, f"{h}:*"]
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=cfg.allowed_origins or [f"https://{h}" for h in cfg.public_hostnames],
    )
    # FastMCP caches its StreamableHTTP session manager, but a session manager's
    # run() works only once per instance — a second build_app (tests, in-process
    # restarts) would fail at startup. Reset the cache so every built app gets
    # its own manager.
    mcp._session_manager = None
    app = mcp.streamable_http_app()
    allowed = set(cfg.allowed_origins)
    return AuthMiddleware(app, token=cfg.app_token, allowed_origins=allowed,
                          pairing_enabled=cfg.pairing_enabled)


def main() -> None:
    import uvicorn

    cfg = load_config()
    logger.info("TaskLane MCP on %s:%s (mcp path /mcp) admin_exec=%s", cfg.mcp_host, cfg.mcp_port, cfg.enable_admin_exec)
    uvicorn.run(build_app(cfg), host=cfg.mcp_host, port=cfg.mcp_port, log_level="info")


if __name__ == "__main__":
    main()
