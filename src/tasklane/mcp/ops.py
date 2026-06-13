"""TaskLane MCP ops tools (registered on the shared FastMCP instance)."""

from __future__ import annotations

from tasklane.mcp.core import *  # noqa: F401,F403 — shared hub (mcp, audited, helpers)
from tasklane.mcp.core import (  # explicit for linters / clarity
    mcp, audited, _cfg, _store, _get_record, _worktree_dir, _readable_dir,
    _safe_path, _run, _summary, _live_worktree, _audit, logger, repo_path_allowed,
    pairing, cleanup_worktree, worktrees_root, _WORKER_SERVICE, _MAX_READ_BYTES, _MAX_OUT,
)


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
def register_project(
    path: str,
    name: str,
    test_command: str | None = None,
    build_command: str | None = None,
    docs: list[str] | None = None,
    base_branch: str = "main",
    default_model: str | None = None,
    merge_policy: str = "manual",
) -> dict[str, Any]:
    """Register/update a project profile so every job on this repo is told its
    test/build commands and which authoritative docs (CLAUDE.md, ADRs, rules dirs)
    it MUST read. path must be an absolute git-repo path within the allowlist.
    merge_policy (manual|auto) is informational. Returns the stored profile."""
    from tasklane.projects import register_project as _register

    cfg = _cfg()
    if not repo_path_allowed(path, cfg):
        raise ValueError(f"repo path not in allowlist: {path}")
    return _register(path, name=name, test_command=test_command, build_command=build_command,
                     docs=docs, base_branch=base_branch, default_model=default_model,
                     merge_policy=merge_policy)


@mcp.tool()
@audited
def list_projects() -> dict[str, Any]:
    """List all registered project profiles as {repo_path: profile}."""
    from tasklane.projects import list_projects as _list

    return _list()


# --------------------------------------------------------------------------- #
# tester roles + conversational credential intake
# --------------------------------------------------------------------------- #
_TEST_MODE_ROLES = {"staging": "test-staging", "local": "test-local"}


@mcp.tool()
@audited
def test_deployment(repo: str, mode: str = "staging", flows: str = "", id: str | None = None) -> dict[str, Any]:
    """Create a report-only TESTER job that verifies a deployment by RUNNING it.

    mode: "staging" drives the deployed frontend with Playwright (test-staging role);
    "local" boots the app in the worktree and exercises it (test-local role). flows is
    free text listing the user flows to verify (goes into the job body). The job's
    project profile (test commands, staging URL, and the secret env_file) is resolved
    automatically from the repo path if the repo is registered. The job is
    detached-review + report-only (never commits or pushes)."""
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
    if id:
        spec["id"] = id
    record = _store().put(spec)
    return {"id": record["id"], "state": record["state"], "role": role, "mode": normalized_mode}


def _require_registered(repo: str) -> dict[str, Any]:
    """Return (canonical) profile for an allowlisted, registered repo or raise."""
    cfg = _cfg()
    if not repo_path_allowed(repo, cfg):
        raise ValueError(f"repo path not in allowlist: {repo}")
    from tasklane.projects import get_project

    profile = get_project(repo)
    if profile is None:
        raise ValueError(f"project not registered: {repo} (call register_project first)")
    return profile


@mcp.tool()
@audited
def set_project_secrets(repo: str, secrets: dict[str, str]) -> dict[str, Any]:
    """Store/merge test credentials for a project's TESTER roles (conversational intake).

    repo must be allowlisted AND registered. secrets is a {KEY: VALUE} mapping: keys
    SCREAMING_SNAKE_CASE (^[A-Z][A-Z0-9_]*$), values non-empty and <=4096 chars, at
    most 50 keys. Pairs are merged into the project's env_file (mode 600); if the
    project has none, ~/.tasklane/secrets/<name>.env (dir 700, file 600) is created
    and the registry entry auto-linked to it.

    SECURITY: never repeat the stored values back to the user. The audit log records
    only repo + sorted KEY NAMES; the return value carries names only, never values."""
    from tasklane.projects import set_project_env_file
    from tasklane.secrets import (
        default_secret_file_path, load_env_file, validate_secret_pairs, write_secret_file,
    )

    profile = _require_registered(repo)
    validate_secret_pairs(secrets)
    env_file = profile.get("env_file")
    existing: dict[str, str] = {}
    if env_file:
        try:
            existing = load_env_file(env_file)
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            existing = {}
    merged = {**existing, **{str(k): str(v) for k, v in secrets.items()}}
    linked = False
    if env_file:
        target = write_secret_file(env_file, merged)
    else:
        target = write_secret_file(default_secret_file_path(profile.get("name") or "project"), merged, secure_parent=True)
        set_project_env_file(repo, str(target))
        linked = True
    return {"repo": repo, "env_file": str(target), "keys_written": sorted(secrets),
            "key_names": sorted(merged), "total_keys": len(merged), "registry_linked": linked}


@mcp.tool()
@audited
def list_project_secret_keys(repo: str) -> dict[str, Any]:
    """List the KEY NAMES (never values) stored in a registered project's env_file."""
    from tasklane.secrets import load_env_file

    profile = _require_registered(repo)
    env_file = profile.get("env_file")
    if not env_file:
        return {"repo": repo, "env_file": None, "key_names": []}
    try:
        values = load_env_file(env_file)
    except (FileNotFoundError, PermissionError, ValueError, OSError) as exc:
        return {"repo": repo, "env_file": env_file, "key_names": [], "error": str(exc)}
    return {"repo": repo, "env_file": env_file, "key_names": sorted(values)}


@mcp.tool()
@audited
def delete_project_secret(repo: str, key: str) -> dict[str, Any]:
    """Remove a single secret KEY from a registered project's env_file."""
    from tasklane.secrets import load_env_file, write_secret_file

    profile = _require_registered(repo)
    env_file = profile.get("env_file")
    if not env_file:
        raise ValueError(f"project has no env_file: {repo}")
    values = load_env_file(env_file)
    if key not in values:
        return {"repo": repo, "env_file": env_file, "removed": False, "key_names": sorted(values)}
    del values[key]
    write_secret_file(env_file, values)
    return {"repo": repo, "env_file": env_file, "removed": True, "key_names": sorted(values)}


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


