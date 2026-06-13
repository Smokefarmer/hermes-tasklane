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
from collections.abc import Mapping
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

# Self-teaching doctrine surfaced to every connecting MCP client (FastMCP
# `instructions`). Keep it compact (<= 60 lines): it loads into the client's
# context on connect, so it must earn its tokens.
USAGE = """\
TaskLane is an autonomous coding-job control plane. You hand it a self-contained
coding task; a server-side worker runs it with `claude -p` in an isolated git
worktree, then commits / pushes / opens a PR. You stay the operator: brief jobs
well, monitor them, and unblock the few that get stuck.

WHEN TO DELEGATE: work that is >30 min of grind, parallelizable across repos, or
best run overnight while you sleep (batch refactors, test backfill, dependency
bumps, a queue of small bugfixes). DO NOT delegate tiny edits you can finish in
the time it takes to write a brief, or anything needing live back-and-forth.

ANATOMY OF A GOOD BRIEF (put it all in `body`): the goal; explicit acceptance
criteria; how to verify (exact test/build command); explicit NON-goals / out of
scope; and the files or modules in scope. Vague briefs produce blocked jobs.

LIFECYCLE: create_task (one job) or create_pipeline (plan -> implement -> review,
dependency-chained, each stage sees the prior stage's output). Then observe with
get_task / task_events / task_logs. Terminal states: completed, blocked, failed,
needs-human. BLOCKED means the job needs YOU: inspect with get_diff / read_file /
exec, repair with write_file / apply_patch / git / run_tests inside the worktree,
then retry_task (add `extra_instructions` to steer the next run).

ETIQUETTE: do NOT poll in a tight loop. Transient failures (workspace prep, agent
errors) auto-retry with backoff; only `blocked` needs a human. Check back on your
own cadence, or watch the /status page. Cancel with cancel_task; nudge a parked
job with run_task_now.

COST: each job records cost/tokens/turns on its record (get_task -> metrics).
Store-wide spend and 24h totals come from the `metrics` tool. A configurable
daily budget pauses new claims when reached, so queue deliberately.
"""

mcp = FastMCP("tasklane", instructions=USAGE)


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


# Kwargs whose VALUES must never reach the audit log. A dict-valued sensitive arg
# (e.g. set_project_secrets(secrets=...)) is logged as its sorted KEY NAMES only;
# a scalar one is logged as "<redacted>". Names are safe (and useful) to audit.
_SENSITIVE_ARGS = {"secrets"}


def _redact_audit_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key in _SENSITIVE_ARGS:
            if isinstance(value, Mapping):
                safe[key] = {"<redacted_keys>": sorted(str(k) for k in value)}
            else:
                safe[key] = "<redacted>"
        else:
            safe[key] = value
    return safe


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
            _audit(fn.__name__, _redact_audit_kwargs(dict(kwargs)), status)
    return wrapper


def _get_record(job_id: str) -> dict[str, Any]:
    record = _store().get(job_id)
    if record is None:
        raise FileNotFoundError(f"job not found: {job_id}")
    return record


def _live_worktree(record: dict[str, Any]) -> Path | None:
    """The job's isolated worktree if it still exists on disk, else None."""
    wt = (record.get("worktree") or {}).get("worktree_path")
    if wt and Path(wt).is_dir():
        return Path(wt)
    return None


def _worktree_dir(record: dict[str, Any]) -> Path:
    """Directory for a MUTATING fix tool. Strictly the job's isolated worktree —
    NEVER falls back to spec.repo.path (the user's real checkout). A job with no
    live worktree (draft / report-only / completed-and-cleaned) refuses to be
    mutated rather than silently editing the original repository."""
    live = _live_worktree(record)
    if live is not None:
        return live
    raise FileNotFoundError(
        f"job {record.get('id')} has no live worktree (state={record.get('state')}); "
        "refusing to operate on the original checkout"
    )


def _readable_dir(record: dict[str, Any]) -> tuple[Path, bool]:
    """Directory for a READ-ONLY tool. Prefers the live worktree; falls back to
    the original checkout when none exists. Returns (path, is_original) so callers
    can label that they are reading the user's real repo, not a sandbox."""
    live = _live_worktree(record)
    if live is not None:
        return live, False
    repo = ((record.get("spec") or {}).get("repo") or {}).get("path")
    if repo and Path(repo).expanduser().is_dir():
        return Path(repo).expanduser(), True
    raise FileNotFoundError(f"no worktree or repo path available for job {record.get('id')} (state={record.get('state')})")


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


# Shared hub: tool submodules do `from tasklane.mcp.core import *` to get the
# FastMCP instance, the audited decorator, the helpers, and the common external
# symbols. Underscore names are listed explicitly so import * re-exports them.
__all__ = [
    "mcp", "logger", "audited", "_audit", "_redact_audit_kwargs",
    "_cfg", "_store", "_get_record", "_live_worktree", "_worktree_dir",
    "_readable_dir", "_safe_path", "_run", "_summary",
    "_WORKER_SERVICE", "_MAX_READ_BYTES", "_MAX_OUT",
    "_MAX_PAIR_BODY_BYTES", "_MAX_CLIENT_NAME_LENGTH", "USAGE",
    # external symbols commonly referenced by tool bodies
    "Config", "load_config", "repo_path_allowed", "JobStore",
    "cleanup_worktree", "audit_log_path", "logs_root", "worktrees_root",
    "pairing", "Path", "Any", "re", "json", "os", "hmac", "subprocess",
    "datetime", "timezone", "Mapping",
]
