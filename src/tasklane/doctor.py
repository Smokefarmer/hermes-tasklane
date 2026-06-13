"""TaskLane diagnostics — ``tasklane doctor``.

Runs a series of read-only health checks over the TaskLane home, config,
job store, required executables, and disk usage. Each check yields a
:class:`Check` (``name``/``status``/``detail``); the aggregate report exits
non-zero only when at least one check ``fail``s. The command never mutates job
state or deletes locks — it points at ``tasklane reconcile`` for that.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tasklane.config import Config, load_config
from tasklane.paths import config_path, tasklane_home, worktrees_root
from tasklane.reconcile import claimant_pid, pid_alive
from tasklane.store import JOB_STATES, JobStore, utc_now

OK = "ok"
WARN = "warn"
FAIL = "fail"

_GIB = 1024 ** 3
_MIN_FREE_GIB = 1.0
_WORKTREES_WARN_GIB = 5.0


@dataclass(frozen=True)
class Check:
    """One diagnostic result."""

    name: str
    status: str  # ok | warn | fail
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def check_home(home: Path) -> Check:
    if not home.exists():
        return Check("tasklane_home", FAIL, f"{home} does not exist")
    if not home.is_dir():
        return Check("tasklane_home", FAIL, f"{home} is not a directory")
    if not os.access(home, os.W_OK):
        return Check("tasklane_home", FAIL, f"{home} is not writable")
    return Check("tasklane_home", OK, f"{home} exists and is writable")


def check_config_parse(path: Path) -> tuple[Config | None, Check]:
    """Load the config, reporting parse failures. Returns the parsed config (or None)."""
    existed = path.exists()
    try:
        cfg = load_config()
    except Exception as exc:  # malformed YAML / non-mapping
        return None, Check("config_parse", FAIL, f"config.yaml failed to parse: {exc}")
    if not existed:
        return cfg, Check("config_parse", OK, "config.yaml created with defaults")
    return cfg, Check("config_parse", OK, "config.yaml parsed")


def check_app_token(cfg: Config) -> Check:
    if not cfg.app_token:
        return Check("config_app_token", FAIL, "app_token is empty — the MCP control plane would be unauthenticated")
    return Check("config_app_token", OK, "app_token is set")


def check_config_permissions(path: Path) -> Check:
    if not path.exists():
        return Check("config_permissions", WARN, f"{path} does not exist")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode == 0o600:
        return Check("config_permissions", OK, "config.yaml mode is 600")
    return Check("config_permissions", WARN, f"config.yaml mode is {mode:03o}, expected 600 (it holds app_token)")


def check_job_store_dirs(store: JobStore) -> Check:
    """Ensure store dirs exist; report any that had to be created."""
    expected = [store.root / state for state in JOB_STATES] + [store.events_dir, store.locks_dir]
    missing = sorted(p.name for p in expected if not p.exists())
    store.ensure_dirs()
    if missing:
        return Check("job_store_dirs", OK, f"created missing job store dirs: {', '.join(missing)}")
    return Check("job_store_dirs", OK, "all job store directories present")


def check_executable(name: str, *, missing_status: str) -> Check:
    location = shutil.which(name)
    if location:
        return Check(f"{name}_cli", OK, f"{name} found at {location}")
    return Check(f"{name}_cli", missing_status, f"{name} not found on PATH")


def check_stale_locks(store: JobStore, cfg: Config) -> Check:
    if not store.locks_dir.exists():
        return Check("stale_locks", OK, "no claim locks")
    cutoff = time.time() - max(0.0, cfg.stale_lock_seconds)
    stale: list[str] = []
    for path in store.locks_dir.glob("*.lock"):
        try:
            if path.stat().st_mtime < cutoff:
                stale.append(path.stem)
        except OSError:
            continue
    if stale:
        detail = (
            f"{len(stale)} stale claim lock(s) older than {int(cfg.stale_lock_seconds)}s "
            f"({', '.join(sorted(stale))}) — run `tasklane reconcile`"
        )
        return Check("stale_locks", WARN, detail)
    return Check("stale_locks", OK, "no stale claim locks")


def check_orphaned_jobs(store: JobStore) -> Check:
    """Flag running jobs whose claimant process is dead (read-only; no transition)."""
    orphans: list[str] = []
    for record in store.list(states=["running"]):
        pid = claimant_pid(record.get("claimed_by"))
        if pid is not None and not pid_alive(pid):
            orphans.append(str(record.get("id") or "?"))
    if orphans:
        detail = (
            f"{len(orphans)} running job(s) with a dead claimant "
            f"({', '.join(sorted(orphans))}) — run `tasklane reconcile`"
        )
        return Check("orphaned_jobs", WARN, detail)
    return Check("orphaned_jobs", OK, "no orphaned running jobs")


def check_disk_space(home: Path, *, min_free_gib: float = _MIN_FREE_GIB) -> Check:
    try:
        usage = shutil.disk_usage(home)
    except OSError as exc:
        return Check("disk_space", WARN, f"could not determine free space: {exc}")
    free_gib = usage.free / _GIB
    detail = f"{free_gib:.1f} GiB free at {home}"
    if free_gib < min_free_gib:
        return Check("disk_space", WARN, f"low disk space — {detail} (under {min_free_gib:.0f} GiB)")
    return Check("disk_space", OK, detail)


def _dir_size_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).lstat().st_size
            except OSError:
                continue
    return total


def check_worktrees_size(root: Path, *, warn_gib: float = _WORKTREES_WARN_GIB) -> Check:
    if not root.exists():
        return Check("worktrees_size", OK, "no worktrees root yet")
    size_gib = _dir_size_bytes(root) / _GIB
    detail = f"worktrees root is {size_gib:.1f} GiB ({root})"
    if size_gib > warn_gib:
        return Check("worktrees_size", WARN, f"{detail} — over {warn_gib:.0f} GiB; prune stale job worktrees")
    return Check("worktrees_size", OK, detail)


def run_checks() -> list[Check]:
    """Run every diagnostic against the live TaskLane home and return results."""
    home = tasklane_home()
    cfg, parse_check = check_config_parse(config_path())
    effective_cfg = cfg if cfg is not None else Config()
    store = JobStore()
    checks = [
        check_home(home),
        parse_check,
        check_app_token(effective_cfg),
        check_config_permissions(config_path()),
        check_job_store_dirs(store),
        check_executable("claude", missing_status=FAIL),
        check_executable("git", missing_status=FAIL),
        check_executable("gh", missing_status=WARN),
        check_stale_locks(store, effective_cfg),
        check_orphaned_jobs(store),
        check_disk_space(home),
        check_worktrees_size(worktrees_root()),
    ]
    return checks


def overall_status(checks: list[Check]) -> str:
    statuses = {c.status for c in checks}
    if FAIL in statuses:
        return FAIL
    if WARN in statuses:
        return WARN
    return OK


def build_report() -> dict[str, Any]:
    checks = run_checks()
    return {
        "at": utc_now(),
        "overall": overall_status(checks),
        "checks": [c.to_dict() for c in checks],
    }


def format_table(report: dict[str, Any]) -> str:
    rows = report.get("checks", [])
    name_width = max((len(r["name"]) for r in rows), default=4)
    lines = [f"{r['status'].upper():<4}  {r['name']:<{name_width}}  {r['detail']}" for r in rows]
    lines.append("")
    lines.append(f"overall: {str(report.get('overall', '?')).upper()}")
    return "\n".join(lines)
