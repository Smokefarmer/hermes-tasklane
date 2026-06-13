"""TaskLane MCP fixtools tools (registered on the shared FastMCP instance)."""

from __future__ import annotations

from tasklane.mcp.core import *  # noqa: F401,F403 — shared hub (mcp, audited, helpers)
from tasklane.mcp.core import (  # explicit for linters / clarity
    mcp, audited, _cfg, _store, _get_record, _worktree_dir, _readable_dir,
    _safe_path, _run, _summary, _live_worktree, _audit, logger, repo_path_allowed,
    pairing, cleanup_worktree, worktrees_root, _WORKER_SERVICE, _MAX_READ_BYTES, _MAX_OUT,
)


# inspect & fix tools (confined to the job's worktree)
# --------------------------------------------------------------------------- #
@mcp.tool()
@audited
def get_diff(job_id: str) -> dict[str, Any]:
    """Show the job worktree's git state: status, unstaged diff, and staged diff."""
    wt, is_original = _readable_dir(_get_record(job_id))
    return {
        "worktree": str(wt),
        "target": "original-repo" if is_original else "worktree",
        "status": _run(["git", "status", "--porcelain", "--branch"], cwd=wt, timeout=30),
        "diff": _run(["git", "diff"], cwd=wt, timeout=30),
        "staged_diff": _run(["git", "diff", "--cached"], cwd=wt, timeout=30),
    }


@mcp.tool()
@audited
def list_dir(job_id: str, path: str = ".") -> dict[str, Any]:
    """List a directory inside the job worktree (or the original repo if no worktree)."""
    wt, is_original = _readable_dir(_get_record(job_id))
    target = _safe_path(wt, path)
    if not target.is_dir():
        raise NotADirectoryError(f"not a directory: {path}")
    entries = []
    for child in sorted(target.iterdir()):
        entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file",
                        "size": child.stat().st_size if child.is_file() else None})
    return {"path": str(target.relative_to(wt.resolve())),
            "target": "original-repo" if is_original else "worktree", "entries": entries}


@mcp.tool()
@audited
def read_file(job_id: str, path: str) -> dict[str, Any]:
    """Read a file inside the job worktree (or the original repo if no worktree; truncated to 200KB)."""
    wt, is_original = _readable_dir(_get_record(job_id))
    target = _safe_path(wt, path)
    if not target.is_file():
        raise FileNotFoundError(f"not a file: {path}")
    data = target.read_bytes()[:_MAX_READ_BYTES]
    return {"path": path, "truncated": target.stat().st_size > _MAX_READ_BYTES,
            "target": "original-repo" if is_original else "worktree",
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
    """Run a shell command with cwd set to the job worktree (bash -lc). NOT a sandbox:
    cwd-scoped only — absolute paths or `cd ..` reach the rest of the filesystem as the
    server user. Gated by the bearer token. Refuses jobs with no live worktree."""
    wt = _worktree_dir(_get_record(job_id))
    t = int(timeout or _cfg().exec_timeout_seconds)
    return _run(["bash", "-lc", command], cwd=wt, timeout=t)


@mcp.tool()
@audited
def git(job_id: str, args: str) -> dict[str, Any]:
    """Run a git command with cwd set to the job worktree (e.g. args='status --short').
    Like `exec`, this is a cwd-scoped `bash -lc` shell, NOT a sandbox. Refuses jobs with
    no live worktree."""
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

