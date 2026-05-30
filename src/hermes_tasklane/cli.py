from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager, nullcontext, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DELIVERY_BLOCKERS = {
    "github-no-write-permission",
    "push-failed",
    "pr-create",
    "pr-already-exists",
    "delivery-failed",
    "ci-pending",
    "ci-unavailable",
}
ACTIVE_RUN_STATES = {"queued", "running"}
ACTIVE_JOB_STATES = {"ready", "running"}
JOB_STATES = {"draft", "ready", "running", "blocked", "completed", "failed", "needs-human"}
TASK_SUFFIXES = {".md", ".txt"}
SALVAGEABLE_DELIVERY_MODES = {"pull-request"}
SAFE_RETRY_ERROR_PATTERNS = (
    "apierror",
    "an error occurred while processing your request",
    "connection reset",
    "gateway stopped",
    "gateway restart",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "internal server error",
    "rate limit",
    "server had an error processing your request",
    "temporary",
    "timeout",
    "timed out",
    "transport",
    "worker lost",
)
STALE_WORKTREE_ERROR_PATTERNS = (
    "work_branch is already checked out in another worktree",
    "already checked out in another worktree",
)
REVIEW_HUMAN_DECISION_PATTERNS = (
    "approval required",
    "cannot proceed without",
    "contract upgrade required",
    "deployer",
    "design decision",
    "manual approval",
    "missing api key",
    "missing product decision",
    "needs product decision",
    "product decision",
    "requires george",
    "secret",
    "what should",
)
REVIEW_EVIDENCE_ONLY_PATTERNS = (
    "diffstat",
    "evidence",
    "exact command",
    "head sha",
    "pr body",
    "test count",
    "verification command",
    "verification evidence",
)
UNSAFE_RETRY_ERROR_PATTERNS = (
    "invalid execution_mode",
    "invalid request_type",
    "no code changes",
    "planning-invalid-packet",
    "worktree has uncommitted",
)
SALVAGE_ERROR_PATTERNS = (
    *SAFE_RETRY_ERROR_PATTERNS,
    "worktree has uncommitted",
    "dirty worktree",
    "uncommitted changes after agent run",
)
CI_FAIL_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
CI_UNAVAILABLE_CONCLUSIONS = {"action_required", "startup_failure"}
CI_UNAVAILABLE_PATTERNS = (
    "actions are disabled",
    "artifact expired",
    "billing",
    "blobnotfound",
    "disabled by billing",
    "minute limit",
    "minutes limit",
    "no hosted parallelism",
    "quota",
    "resource not accessible",
    "spending limit",
    "startup failure",
    "usage limit",
    "workflow was not run",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def run_process(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def run_shell_command(command: str, *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        shell=True,
        timeout=timeout,
    )


def output_tail(text: str, limit: int = 1200) -> str:
    return text[-limit:] if len(text) > limit else text


def git_worktree_prune(repo_path: Path) -> dict[str, Any]:
    repo_path = repo_path.expanduser()
    if not repo_path.exists():
        return {"ok": False, "reason": "repo-missing", "repo_path": str(repo_path)}
    probe = run_process(["git", "-C", str(repo_path), "rev-parse", "--git-common-dir"], timeout=30)
    if probe.returncode != 0:
        return {"ok": True, "reason": "not-a-git-repo", "repo_path": str(repo_path)}
    proc = run_process(["git", "-C", str(repo_path), "worktree", "prune"], timeout=120)
    return {
        "ok": proc.returncode == 0,
        "reason": "pruned" if proc.returncode == 0 else "git-worktree-prune-failed",
        "repo_path": str(repo_path),
        "returncode": proc.returncode,
        "stdout_tail": output_tail(proc.stdout),
        "stderr_tail": output_tail(proc.stderr),
    }


def git_worktree_branch_entries(repo_path: Path, branch: str) -> dict[str, Any]:
    if not branch:
        return {"ok": False, "reason": "branch-missing", "entries": []}
    repo_path = repo_path.expanduser()
    if not repo_path.exists():
        return {"ok": False, "reason": "repo-missing", "repo_path": str(repo_path), "entries": []}
    proc = run_process(["git", "-C", str(repo_path), "worktree", "list", "--porcelain"], timeout=120)
    if proc.returncode != 0:
        return {
            "ok": False,
            "reason": "git-worktree-list-failed",
            "repo_path": str(repo_path),
            "entries": [],
            "returncode": proc.returncode,
            "stdout_tail": output_tail(proc.stdout),
            "stderr_tail": output_tail(proc.stderr),
        }
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                entries.append(current)
            current = {"worktree": value}
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "prunable":
            current["prunable"] = value or True
    if current:
        entries.append(current)
    matches = [entry for entry in entries if entry.get("branch") == branch]
    return {"ok": True, "repo_path": str(repo_path), "branch": branch, "entries": matches}


def prune_task_repos(tasks: list[TaskFile]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in tasks:
        key = str(task.repo_path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        results.append(git_worktree_prune(task.repo_path))
    return results


@dataclass
class Config:
    hermes_home: Path
    task_root: Path
    poll_repo_idle: bool
    max_pending_per_repo: int
    github_owner_hint: str | None
    default_platform: str | None
    default_chat_id: str | None
    default_thread_id: str | None
    watch: dict[str, Any]
    review_gate: dict[str, Any]
    wave_planner: dict[str, Any]

    @property
    def inbox_dir(self) -> Path:
        return self.task_root / "inbox"

    @property
    def submitted_dir(self) -> Path:
        return self.task_root / "submitted"

    @property
    def completed_dir(self) -> Path:
        return self.task_root / "completed"

    @property
    def failed_dir(self) -> Path:
        return self.task_root / "failed"

    @property
    def cancelled_dir(self) -> Path:
        return self.task_root / "cancelled"

    @property
    def state_path(self) -> Path:
        return self.task_root / "state.json"

    @property
    def lock_path(self) -> Path:
        return self.task_root / ".tasklane.lock"

    @property
    def jobs_dir(self) -> Path:
        return self.hermes_home / "jobs"

    @property
    def job_events_dir(self) -> Path:
        return self.jobs_dir / "events"

    @property
    def job_locks_dir(self) -> Path:
        return self.jobs_dir / "locks"

    @property
    def runs_dir(self) -> Path:
        return self.hermes_home / "runs"

    @property
    def events_dir(self) -> Path:
        return self.runs_dir / "events"

    @property
    def repo_locks_dir(self) -> Path:
        return self.runs_dir / "repo-locks"


def default_config_path() -> Path:
    return Path.home() / ".config" / "hermes-tasklane" / "config.json"


def load_config(explicit_path: str | None = None) -> Config:
    path = Path(explicit_path).expanduser() if explicit_path else default_config_path()
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        raw = {}
    hermes_home = Path(raw.get("hermes_home") or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
    task_root = Path(raw.get("task_root") or (Path.home() / ".local" / "share" / "hermes-tasklane")).expanduser()
    return Config(
        hermes_home=hermes_home,
        task_root=task_root,
        poll_repo_idle=bool(raw.get("poll_repo_idle", True)),
        max_pending_per_repo=int(raw.get("max_pending_per_repo", 1)),
        github_owner_hint=raw.get("github_owner_hint"),
        default_platform=raw.get("default_platform"),
        default_chat_id=raw.get("default_chat_id"),
        default_thread_id=raw.get("default_thread_id"),
        watch=dict(raw.get("watch") or {}),
        review_gate=dict(raw.get("review_gate") or {}),
        wave_planner=dict(raw.get("wave_planner") or {}),
    )


def ensure_layout(cfg: Config) -> None:
    for path in [
        cfg.inbox_dir,
        cfg.submitted_dir,
        cfg.completed_dir,
        cfg.failed_dir,
        cfg.cancelled_dir,
        cfg.runs_dir,
        cfg.job_events_dir,
        cfg.job_locks_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)
    for state in JOB_STATES:
        (cfg.jobs_dir / state).mkdir(parents=True, exist_ok=True)


@contextmanager
def tasklane_lock(cfg: Config):
    cfg.task_root.mkdir(parents=True, exist_ok=True)
    with cfg.lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_state(cfg: Config) -> dict[str, Any]:
    if not cfg.state_path.exists():
        return {"submitted": {}}
    return json.loads(cfg.state_path.read_text(encoding="utf-8"))


def save_state(cfg: Config, state: dict[str, Any]) -> None:
    atomic_write_json(cfg.state_path, state)


def sha_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    stripped = text.strip()
    if not stripped.startswith("---\n"):
        return {}, text.strip()
    parts = stripped.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, text.strip()
    frontmatter, body = parts
    meta: dict[str, str] = {}
    for line in frontmatter.splitlines()[1:]:
        raw = line.strip()
        if not raw or raw.startswith("#") or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        meta[key.strip().lower()] = value.strip()
    return meta, body.strip()


def slugify(value: str, fallback: str = "task") -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-._") or fallback


def normalize_choice(value: str | None, aliases: dict[str, str], default: str) -> str:
    if not value:
        return default
    key = " ".join(value.strip().lower().replace("_", " ").replace("-", " ").split()).replace(" ", "-")
    if key not in aliases:
        raise ValueError(f"unsupported value {value!r}; expected one of: {', '.join(sorted(set(aliases.values())))}")
    return aliases[key]


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    text = value.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value {value!r}")


def bool_from_any(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return parse_bool(str(value), default=default)


REQUEST_TYPE_ALIASES = {
    "bug": "bug-small",
    "bugfix": "bug-small",
    "bug-small": "bug-small",
    "task": "task-small",
    "task-small": "task-small",
    "feature": "feature-large",
    "feature-large": "feature-large",
    "refactor": "refactor-large",
    "refactor-large": "refactor-large",
}

BRANCH_MODE_ALIASES = {
    "new": "new-branch",
    "new-branch": "new-branch",
    "existing": "existing-branch",
    "existing-branch": "existing-branch",
    "detached": "detached-review",
    "detached-review": "detached-review",
    "review": "detached-review",
}

DELIVERY_MODE_ALIASES = {
    "direct": "direct-push",
    "direct-push": "direct-push",
    "push": "direct-push",
    "pr": "pull-request",
    "pull-request": "pull-request",
    "pullrequest": "pull-request",
    "pr-required": "pull-request",
    "report": "report-only",
    "report-only": "report-only",
    "report-only-allowed": "report-only",
}


@dataclass
class TaskFile:
    uid: str
    path: Path
    repo_path: Path
    base_branch: str
    work_branch: str
    branch_mode: str
    delivery_mode: str
    request_type: str
    prompt: str
    metadata: dict[str, Any]
    platform: str | None
    chat_id: str | None
    thread_id: str | None
    project: str | None
    title: str
    allowed_paths: list[str]
    denied_paths: list[str]
    allow_unlisted_paths: bool
    review_loops: int
    security_review: bool
    codex_review: bool
    dependencies: list[str]
    delivery_group: str | None


def load_task_file(path: Path, cfg: Config | None = None) -> TaskFile:
    text = path.read_text(encoding="utf-8")
    meta, prompt = parse_frontmatter(text)
    if not prompt:
        raise ValueError(f"Task body is empty: {path}")
    repo_path = Path(meta.get("repo_path") or "").expanduser()
    if not repo_path:
        raise ValueError(f"repo_path missing in {path}")
    base_branch = meta.get("branch_base") or meta.get("base_branch") or "main"
    uid = meta.get("id") or f"{path.stem}-{sha_id(str(path.resolve()))}"
    branch_mode = normalize_choice(meta.get("branch_mode") or meta.get("mode"), BRANCH_MODE_ALIASES, "new-branch")
    delivery_mode = normalize_choice(meta.get("delivery_mode") or meta.get("delivery"), DELIVERY_MODE_ALIASES, "pull-request")
    request_type = normalize_choice(meta.get("request_type") or meta.get("type"), REQUEST_TYPE_ALIASES, "task-small")
    work_branch = meta.get("work_branch") or meta.get("working_branch") or meta.get("branch")
    delivery_group = meta.get("delivery_group") or meta.get("pr_group") or meta.get("epic")
    if not work_branch and delivery_group and branch_mode in {"new-branch", "existing-branch"}:
        work_branch = f"tasklane/{slugify(delivery_group)}"
    if not work_branch and branch_mode == "new-branch":
        work_branch = f"tasklane/{slugify(path.stem)}-{sha_id(uid)}"
    if not work_branch and branch_mode == "existing-branch":
        raise ValueError(f"work_branch is required for existing-branch tasks: {path}")
    if delivery_mode == "pull-request" and branch_mode == "detached-review":
        raise ValueError(f"pull-request delivery is not valid for detached-review tasks: {path}")
    return TaskFile(
        uid=uid,
        path=path,
        repo_path=repo_path,
        base_branch=base_branch,
        work_branch=work_branch or "",
        branch_mode=branch_mode,
        delivery_mode=delivery_mode,
        request_type=request_type,
        prompt=prompt,
        metadata={
            k: v
            for k, v in meta.items()
            if k
            not in {
                "repo_path",
                "branch_base",
                "base_branch",
                "work_branch",
                "working_branch",
                "branch",
                "branch_mode",
                "mode",
                "delivery_mode",
                "delivery",
                "request_type",
                "type",
                "platform",
                "chat_id",
                "thread_id",
                "project",
                "id",
                "title",
                "allowed_paths",
                "denied_paths",
                "allow_unlisted_paths",
                "review_loops",
                "security_review",
                "codex_review",
                "review_gate",
                "dependencies",
                "depends_on",
                "delivery_group",
                "pr_group",
                "epic",
            }
        },
        platform=meta.get("platform") or (cfg.default_platform if cfg else None),
        chat_id=meta.get("chat_id") or (cfg.default_chat_id if cfg else None),
        thread_id=meta.get("thread_id") or (cfg.default_thread_id if cfg else None),
        project=meta.get("project"),
        title=meta.get("title") or path.stem.replace("-", " ").replace("_", " ").strip() or uid,
        allowed_paths=parse_csv(meta.get("allowed_paths")),
        denied_paths=parse_csv(meta.get("denied_paths")),
        allow_unlisted_paths=parse_bool(meta.get("allow_unlisted_paths"), default=True),
        review_loops=int(meta.get("review_loops") or 3),
        security_review=parse_bool(meta.get("security_review"), default=True),
        codex_review=parse_bool(
            meta.get("codex_review") or meta.get("review_gate"),
            default=bool_from_any((cfg.review_gate if cfg else {}).get("enabled"), default=False),
        ),
        dependencies=parse_csv(meta.get("dependencies") or meta.get("depends_on")),
        delivery_group=delivery_group,
    )


def canonical_repo_path(repo_path: Path) -> Path:
    repo_path = repo_path.resolve()
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).resolve()
    except Exception:
        pass
    return repo_path


def repo_key(repo_path: Path) -> str:
    return f"repo://{canonical_repo_path(repo_path)}"


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def active_runs_for_repo(cfg: Config, expected_repo_key: str) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for path in cfg.runs_dir.glob("*.json"):
        payload = load_json(path)
        if not isinstance(payload, dict):
            continue
        if payload.get("kind") != "coding_task":
            continue
        if str(payload.get("state") or "").strip().lower() not in ACTIVE_RUN_STATES:
            continue
        if ((payload.get("repo") or {}).get("key") or "") == expected_repo_key:
            active.append(payload)
    return active


def repo_lock_exists(cfg: Config, expected_repo_key: str) -> bool:
    digest = hashlib.sha256(expected_repo_key.encode("utf-8")).hexdigest()[:16]
    return (cfg.repo_locks_dir / f"{digest}.json").exists()


def job_path(cfg: Config, job_id: str, state: str) -> Path:
    return cfg.jobs_dir / state / f"{job_id}.json"


def job_event_log_path(cfg: Config, job_id: str) -> Path:
    return cfg.job_events_dir / f"{job_id}.jsonl"


def iter_job_records(cfg: Config, states: set[str] | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    selected = states or JOB_STATES
    for state in selected:
        directory = cfg.jobs_dir / state
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            payload = load_json(path)
            if isinstance(payload, dict):
                records.append(payload)
    return records


def completed_job_ids(cfg: Config) -> set[str]:
    return {
        str(payload.get("id") or "")
        for payload in iter_job_records(cfg, {"completed"})
        if payload.get("id")
    }


def job_dependencies(payload: dict[str, Any]) -> list[str]:
    spec = payload.get("spec") or {}
    dependencies = spec.get("dependencies") or []
    if isinstance(dependencies, str):
        dependencies = parse_csv(dependencies)
    if not isinstance(dependencies, list):
        return []
    return [str(item).strip() for item in dependencies if str(item).strip()]


def waiting_dependencies(payload: dict[str, Any], completed: set[str]) -> list[str]:
    return [dependency for dependency in job_dependencies(payload) if dependency not in completed]


def split_ready_jobs(cfg: Config, ready_jobs: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    completed = completed_job_ids(cfg)
    runnable: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    waiting_for: dict[str, list[str]] = {}
    for job in ready_jobs if ready_jobs is not None else iter_job_records(cfg, {"ready"}):
        missing = waiting_dependencies(job, completed)
        if missing:
            waiting.append(job)
            waiting_for[str(job.get("id") or "")] = missing
        else:
            runnable.append(job)
    return runnable, waiting, waiting_for


def active_jobs_for_repo(cfg: Config, expected_repo_key: str) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for payload in iter_job_records(cfg, ACTIVE_JOB_STATES):
        spec = payload.get("spec") or {}
        if ((spec.get("repo") or {}).get("key") or "") == expected_repo_key:
            active.append(payload)
    return active


def find_job_record(cfg: Config, job_id: str) -> dict[str, Any] | None:
    for state in JOB_STATES:
        payload = load_json(job_path(cfg, job_id, state))
        if isinstance(payload, dict):
            return payload
    return None


def find_job_record_path(cfg: Config, job_id: str) -> Path | None:
    for state in JOB_STATES:
        path = job_path(cfg, job_id, state)
        if path.exists():
            return path
    return None


def read_job_events(cfg: Config, job_id: str) -> list[dict[str, Any]]:
    path = job_event_log_path(cfg, job_id)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def task_job_id(task: TaskFile) -> str:
    return f"tasklane_{sha_id(task.uid)}"


def submitted_dependency_ids(submitted: dict[str, Any]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for uid, entry in submitted.items():
        if not isinstance(entry, dict):
            continue
        job_id = str(entry.get("job_id") or entry.get("run_id") or "").strip()
        if job_id:
            resolved[str(uid)] = job_id
    return resolved


def dependency_job_ids(task: TaskFile, uid_to_job_id: dict[str, str]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for dependency in task.dependencies:
        job_id = uid_to_job_id.get(dependency, dependency)
        if job_id and job_id not in seen:
            resolved.append(job_id)
            seen.add(job_id)
    return resolved


def job_spec(task: TaskFile, job_id: str) -> dict[str, Any]:
    repo_root = canonical_repo_path(task.repo_path)
    source: dict[str, Any] = {
        "type": "tasklane-file",
        "label": task.project or repo_root.name,
        "task_file": str(task.path),
    }
    if task.platform and task.chat_id:
        source["type"] = task.platform
        source["chat_id"] = task.chat_id
        if task.thread_id:
            source["thread_id"] = task.thread_id
    return {
        "schema_version": 1,
        "id": job_id,
        "source": source,
        "project": task.project or repo_root.name,
        "repo": {
            "key": repo_key(repo_root),
            "path": str(repo_root),
        },
        "request": {
            "type": task.request_type,
            "title": task.title,
            "body": task.prompt,
        },
        "branch": {
            "mode": task.branch_mode,
            "base_branch": task.base_branch or None,
            "work_branch": task.work_branch or None,
            "pr_target": task.base_branch if task.delivery_mode == "pull-request" else None,
        },
        "delivery_mode": task.delivery_mode,
        "dependencies": task.dependencies,
        "pipeline": {
            "budgets": {"review_loops": task.review_loops},
            "security_review": task.security_review,
            "codex_review": task.codex_review,
        },
        "scope": {
            "allowed_paths": task.allowed_paths,
            "denied_paths": task.denied_paths,
            "allow_unlisted_paths": task.allow_unlisted_paths,
        },
        "metadata": {
            "source": "tasklane-file-bridge",
            "uid": task.uid,
            "summary": task.path.stem,
            "delivery_group": task.delivery_group,
            **task.metadata,
        },
    }


def job_record(task: TaskFile, job_id: str) -> dict[str, Any]:
    spec = job_spec(task, job_id)
    timestamp = now_iso()
    return {
        "id": job_id,
        "state": "ready",
        "spec": spec,
        "created_at": timestamp,
        "updated_at": timestamp,
        "attempt": 0,
        "last_error": None,
    }


def synthetic_job_record(job_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    timestamp = now_iso()
    return {
        "id": job_id,
        "state": "ready",
        "spec": spec,
        "created_at": timestamp,
        "updated_at": timestamp,
        "attempt": 0,
        "last_error": None,
    }


def review_gate_enabled_for_task(task: TaskFile) -> bool:
    return bool(task.codex_review and task.delivery_mode == "pull-request" and task.work_branch)


def review_job_id(root_uid: str, iteration: int) -> str:
    return f"tasklane_{sha_id(f'{root_uid}:codex-review:{iteration}')}"


def fix_job_id(root_uid: str, iteration: int) -> str:
    return f"tasklane_{sha_id(f'{root_uid}:codex-fix:{iteration}')}"


def merge_job_id(root_uid: str) -> str:
    return f"tasklane_{sha_id(f'{root_uid}:merge-gate')}"


def project_wave_settings(cfg: Config, project: str | None) -> dict[str, Any]:
    raw = cfg.wave_planner or {}
    projects = raw.get("projects") if isinstance(raw.get("projects"), dict) else {}
    if project and isinstance(projects.get(project), dict):
        return dict(projects[project])
    return {}


def project_review_docs(cfg: Config | None, project: str | None) -> list[str]:
    if cfg is None:
        return []
    project_settings = project_wave_settings(cfg, project)
    return string_list(project_settings.get("review_docs") or (cfg.wave_planner or {}).get("review_docs"))


def project_int_setting(cfg: Config, project: str | None, key: str, *, default: int = 0) -> int:
    project_settings = project_wave_settings(cfg, project)
    value = project_settings.get(key)
    if value is None:
        value = (cfg.wave_planner or {}).get(key)
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def project_verification_commands(cfg: Config, project: str | None, profile_name: str | None) -> list[str]:
    profiles = cfg.watch.get("verification_profiles") or {}
    for key in (project, profile_name):
        if key and isinstance(profiles, dict) and key in profiles:
            return string_list(profiles[key])
    return string_list(cfg.watch.get("verification_commands"))


def format_required_verification(commands: list[str]) -> str:
    if not commands:
        return "- No project-specific verification commands are configured; run the strongest relevant tests for touched areas."
    return "\n".join(f"- `{command}`" for command in commands)


def format_review_docs(docs: list[str]) -> str:
    if not docs:
        return "- AGENTS.md\n- relevant docs and developer guides"
    return "\n".join(f"- {doc}" for doc in docs)


def codex_review_prompt(task: TaskFile, root_job_id: str, iteration: int) -> str:
    docs = parse_csv(str(task.metadata.get("review_docs") or ""))
    return "\n".join(
        [
            "Codex PR review gate.",
            "",
            "Review the implementation independently. Do not edit files, commit, push, merge, deploy, or open another PR.",
            f"Implementation job: {root_job_id}",
            f"Base branch: {task.base_branch}",
            f"Work branch: {task.work_branch}",
            "",
            "Mandatory review context:",
            format_review_docs(docs),
            "",
            "Check the diff against the original task, acceptance criteria, repository conventions, tests, scope boundaries, and obvious security or data-loss risks.",
            "Inspect unresolved GitHub PR review threads and comments. Treat unresolved P0/P1/P2 findings as needs-fix unless the latest diff clearly makes the comment obsolete; if obsolete, say why.",
            "Verify that the PR body evidence matches the current head SHA, current diffstat, exact required commands, and current test counts.",
            "Run only read-only inspection and the strongest practical verification commands needed to support the review.",
            "",
            "Return one exact decision line near the top:",
            "TASKLANE_REVIEW_DECISION: pass",
            "or",
            "TASKLANE_REVIEW_DECISION: needs-fix",
            "",
            "Use needs-fix for correctness bugs, missing acceptance criteria, missing required tests, broken build/typecheck/lint, unsafe behavior, or scope creep.",
            "After the decision line, include findings with file/line references where possible, verification evidence, and residual risks.",
            "",
            "Original task:",
            task.prompt,
        ]
    )


def review_gate_spec(task: TaskFile, root_job_id: str, iteration: int, dependency_job_id: str) -> dict[str, Any]:
    repo_root = canonical_repo_path(task.repo_path)
    job_id = review_job_id(task.uid, iteration)
    return {
        "schema_version": 1,
        "id": job_id,
        "source": {"type": "tasklane-review-gate", "label": task.project or repo_root.name, "task_file": str(task.path)},
        "project": task.project or repo_root.name,
        "repo": {"key": repo_key(repo_root), "path": str(repo_root)},
        "request": {"type": "task-small", "title": f"Review {task.title}", "body": codex_review_prompt(task, root_job_id, iteration)},
        "branch": {"mode": "existing-branch", "base_branch": task.base_branch or None, "work_branch": task.work_branch or None, "pr_target": None},
        "delivery_mode": "report-only",
        "dependencies": [dependency_job_id],
        "pipeline": {"role": "codex-review-gate", "budgets": {"review_loops": task.review_loops}, "security_review": task.security_review},
        "scope": {"allowed_paths": task.allowed_paths, "denied_paths": task.denied_paths, "allow_unlisted_paths": task.allow_unlisted_paths},
        "metadata": {
            "source": "tasklane-review-gate",
            "uid": f"{task.uid}:codex-review:{iteration}",
            "root_uid": task.uid,
            "root_job_id": root_job_id,
            "review_iteration": iteration,
            "delivery_group": task.delivery_group,
        },
    }


def run_path(cfg: Config, run_id: str) -> Path:
    return cfg.runs_dir / f"{run_id}.json"


def event_log_path(cfg: Config, run_id: str) -> Path:
    return cfg.events_dir / f"{run_id}.jsonl"


def preflight_report_path(task_path: Path) -> Path:
    return task_path.with_name(f"{task_path.name}.preflight.json")


def clear_preflight_report(task_path: Path) -> None:
    try:
        preflight_report_path(task_path).unlink()
    except FileNotFoundError:
        pass


def write_preflight_report(task: TaskFile, findings: list[dict[str, Any]]) -> None:
    payload = {
        "task": task.path.name,
        "uid": task.uid,
        "status": "blocked",
        "generated_at": now_iso(),
        "findings": findings,
    }
    atomic_write_json(preflight_report_path(task.path), payload)


def preflight_blocker(code: str, message: str) -> dict[str, Any]:
    return {"severity": "blocker", "code": code, "message": message}


def task_is_large(task: TaskFile) -> bool:
    return task.request_type in {"feature-large", "refactor-large"}


def task_mutates_repo(task: TaskFile) -> bool:
    return task.delivery_mode in {"pull-request", "direct-push"} and task.branch_mode != "detached-review"


def dependency_cycle_uids(tasks_by_uid: dict[str, TaskFile]) -> set[str]:
    graph = {
        uid: [dep for dep in task.dependencies if dep in tasks_by_uid]
        for uid, task in tasks_by_uid.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()
    cyclic: set[str] = set()

    def visit(uid: str, stack: list[str]) -> None:
        if uid in visited:
            return
        if uid in visiting:
            try:
                start = stack.index(uid)
                cyclic.update(stack[start:])
            except ValueError:
                cyclic.add(uid)
            return
        visiting.add(uid)
        for dep in graph.get(uid, []):
            visit(dep, [*stack, dep])
        visiting.remove(uid)
        visited.add(uid)

    for uid in graph:
        visit(uid, [uid])
    return cyclic


def preflight_task_batch(loaded_tasks: dict[Path, TaskFile], submitted: dict[str, Any]) -> dict[Path, list[dict[str, Any]]]:
    findings: dict[Path, list[dict[str, Any]]] = {path: [] for path in loaded_tasks}
    tasks_by_uid = {task.uid: task for task in loaded_tasks.values()}
    submitted_uid_to_job_id = submitted_dependency_ids(submitted)

    for path, task in loaded_tasks.items():
        if not task.repo_path.exists():
            findings[path].append(preflight_blocker("repo-path-missing", f"repo_path does not exist: {task.repo_path}"))
        if task.delivery_mode == "direct-push" and task_is_large(task):
            findings[path].append(preflight_blocker("direct-push-large-task", "feature-large/refactor-large tasks must use pull-request delivery"))
        if task.branch_mode == "detached-review" and task.delivery_mode != "report-only":
            findings[path].append(preflight_blocker("detached-review-mutating-delivery", "detached-review tasks must use report-only delivery"))
        if task_is_large(task) and task.allow_unlisted_paths and not task.allowed_paths:
            findings[path].append(preflight_blocker("large-task-unbounded-scope", "large tasks must declare allowed_paths or set allow_unlisted_paths: false"))
        if not task.allow_unlisted_paths and not task.allowed_paths:
            findings[path].append(preflight_blocker("empty-restricted-scope", "allow_unlisted_paths: false requires at least one allowed_paths entry"))
        if task.review_loops < 1 or task.review_loops > 3:
            findings[path].append(preflight_blocker("review-loops-out-of-range", "review_loops must be between 1 and 3"))
        for dependency in task.dependencies:
            if dependency in tasks_by_uid or dependency in submitted_uid_to_job_id or dependency.startswith("tasklane_"):
                continue
            findings[path].append(preflight_blocker("dependency-not-found", f"dependency {dependency!r} is not in this batch or submitted state"))

    for uid in dependency_cycle_uids(tasks_by_uid):
        task = tasks_by_uid[uid]
        findings[task.path].append(preflight_blocker("dependency-cycle", "task dependencies contain a cycle"))

    delivery_groups: dict[tuple[str, str], list[TaskFile]] = {}
    branch_groups: dict[tuple[str, str], list[TaskFile]] = {}
    for task in loaded_tasks.values():
        repo = repo_key(task.repo_path)
        if task.delivery_group:
            delivery_groups.setdefault((repo, task.delivery_group), []).append(task)
        if task_mutates_repo(task) and task.work_branch:
            branch_groups.setdefault((repo, task.work_branch), []).append(task)

    for (_repo, group), tasks in delivery_groups.items():
        bases = {task.base_branch for task in tasks if task.base_branch}
        if len(bases) > 1:
            for task in tasks:
                findings[task.path].append(
                    preflight_blocker(
                        "delivery-group-mixed-base-branches",
                        f"delivery_group {group!r} mixes base branches: {', '.join(sorted(bases))}",
                    )
                )

    for (_repo, branch), tasks in branch_groups.items():
        if len(tasks) < 2:
            continue
        task_uids = {task.uid for task in tasks}
        roots = [task for task in tasks if not any(dep in task_uids for dep in task.dependencies)]
        if len(roots) > 1:
            root_names = ", ".join(sorted(task.uid for task in roots))
            for task in roots:
                findings[task.path].append(
                    preflight_blocker(
                        "same-branch-multiple-roots",
                        f"multiple tasks mutate {branch!r} without an ordering dependency: {root_names}",
                    )
                )

    return findings


def move_task_file(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    destination = dest_dir / src.name
    if destination.exists():
        stamped = dest_dir / f"{datetime.now().strftime('%Y%m%dT%H%M%SZ')}_{src.name}"
        destination = stamped
    shutil.move(str(src), str(destination))
    return destination


def command_doctor(cfg: Config) -> int:
    ensure_layout(cfg)
    report = {
        "hermes_home": str(cfg.hermes_home),
        "task_root": str(cfg.task_root),
        "jobs_ready_exists": (cfg.jobs_dir / "ready").exists(),
        "jobs_running_exists": (cfg.jobs_dir / "running").exists(),
        "job_events_exists": cfg.job_events_dir.exists(),
        "runs_exists": cfg.runs_dir.exists(),
        "repo_locks_exists": cfg.repo_locks_dir.exists(),
        "inbox_files": len(list(cfg.inbox_dir.glob("*.md"))) + len(list(cfg.inbox_dir.glob("*.txt"))),
    }
    print(json.dumps(report, indent=2))
    return 0


def command_sync(cfg: Config) -> int:
    ensure_layout(cfg)
    state = load_state(cfg)
    submitted = dict(state.get("submitted") or {})
    actions: list[dict[str, Any]] = []
    task_paths = sorted([p for p in cfg.inbox_dir.iterdir() if p.is_file() and p.suffix.lower() in TASK_SUFFIXES])
    loaded_tasks: dict[Path, TaskFile] = {}
    for path in task_paths:
        try:
            task = load_task_file(path, cfg)
        except Exception as exc:
            actions.append({"task": path.name, "status": "invalid", "error": str(exc)})
            continue
        loaded_tasks[path] = task
    for prune_result in prune_task_repos(list(loaded_tasks.values())):
        if not prune_result.get("ok"):
            actions.append({"status": "repo-worktree-prune-failed", **prune_result})
    preflight_findings = preflight_task_batch(loaded_tasks, submitted)
    batch_uid_to_job_id = {task.uid: task_job_id(task) for task in loaded_tasks.values()}
    uid_to_job_id = submitted_dependency_ids(submitted)
    uid_to_job_id.update(batch_uid_to_job_id)
    batch_job_ids = set(batch_uid_to_job_id.values())
    pending_by_repo: dict[str, int] = {}
    completed_ids = completed_job_ids(cfg)
    for path, task in loaded_tasks.items():
        if task.uid in submitted:
            actions.append({"task": path.name, "status": "already-submitted", "run_id": submitted[task.uid].get("run_id")})
            continue
        blockers = preflight_findings.get(path) or []
        if blockers:
            write_preflight_report(task, blockers)
            actions.append({"task": path.name, "status": "preflight-blocked", "findings": blockers})
            continue
        clear_preflight_report(path)
        expected_repo_key = repo_key(task.repo_path)
        job_id = batch_uid_to_job_id[task.uid]
        task.dependencies = dependency_job_ids(task, uid_to_job_id)
        task_is_waiting = any(dependency not in completed_ids for dependency in task.dependencies)
        if cfg.poll_repo_idle:
            dependency_ids = set(task.dependencies)
            active = [
                run
                for run in active_runs_for_repo(cfg, expected_repo_key)
                if str(run.get("id") or "") not in dependency_ids
            ]
            active_jobs = [
                job
                for job in active_jobs_for_repo(cfg, expected_repo_key)
                if job.get("id") not in batch_job_ids and str(job.get("id") or "") not in dependency_ids
            ]
            if not task_is_waiting:
                if active:
                    actions.append({"task": path.name, "status": "deferred", "reason": "repo-active-run", "run_ids": [item.get("id") for item in active]})
                    continue
                if active_jobs:
                    actions.append({"task": path.name, "status": "deferred", "reason": "repo-active-job", "job_ids": [item.get("id") for item in active_jobs]})
                    continue
                if repo_lock_exists(cfg, expected_repo_key):
                    actions.append({"task": path.name, "status": "deferred", "reason": "repo-lock-active"})
                    continue
                current_pending = len(active) + len(active_jobs) + pending_by_repo.get(expected_repo_key, 0)
                if current_pending >= cfg.max_pending_per_repo:
                    actions.append(
                        {
                            "task": path.name,
                            "status": "deferred",
                            "reason": "repo-pending-limit",
                            "repo_key": expected_repo_key,
                            "max_pending_per_repo": cfg.max_pending_per_repo,
                        }
                    )
                    continue
        record = job_record(task, job_id)
        ready_path = job_path(cfg, job_id, "ready")
        if find_job_record(cfg, job_id):
            actions.append({"task": path.name, "status": "already-job-recorded", "job_id": job_id})
            continue
        review_enabled = review_gate_enabled_for_task(task)
        review_id = review_job_id(task.uid, 1) if review_enabled else None
        if review_id:
            batch_job_ids.add(review_id)
        atomic_write_json(ready_path, record)
        append_jsonl(job_event_log_path(cfg, job_id), {"timestamp": now_iso(), "job_id": job_id, "event_type": "job_created", "state": "ready", "reason": "tasklane-sync", "metadata": {"source_file": str(path)}})
        if review_enabled and review_id:
            review_spec = review_gate_spec(task, job_id, 1, job_id)
            review_path = job_path(cfg, review_id, "ready")
            atomic_write_json(review_path, synthetic_job_record(review_id, review_spec))
            append_jsonl(
                job_event_log_path(cfg, review_id),
                {
                    "timestamp": now_iso(),
                    "job_id": review_id,
                    "event_type": "job_created",
                    "state": "ready",
                    "reason": "tasklane-sync-review-gate",
                    "metadata": {"root_job_id": job_id, "source_file": str(path)},
                },
            )
        new_task_path = move_task_file(path, cfg.submitted_dir)
        submitted[task.uid] = {
            "source_path": str(new_task_path),
            "original_name": path.name,
            "job_id": job_id,
            "run_id": job_id,
            "job_file": str(ready_path),
            "repo_key": record["spec"]["repo"]["key"],
            "submitted_at": now_iso(),
        }
        if review_enabled and review_id:
            submitted[task.uid]["review_gate"] = {
                "enabled": True,
                "status": "pending",
                "max_loops": task.review_loops,
                "review_job_id": review_id,
                "current_iteration": 1,
                "work_branch": task.work_branch,
                "base_branch": task.base_branch,
            }
            submitted[f"{task.uid}:codex-review:1"] = {
                "synthetic": True,
                "kind": "codex-review",
                "root_uid": task.uid,
                "job_id": review_id,
                "run_id": review_id,
                "repo_key": record["spec"]["repo"]["key"],
                "submitted_at": now_iso(),
                "review_iteration": 1,
            }
        if not task_is_waiting:
            pending_by_repo[expected_repo_key] = pending_by_repo.get(expected_repo_key, 0) + 1
        action = {"task": path.name, "status": "job-ready", "job_id": job_id, "job_file": str(ready_path)}
        if review_enabled and review_id:
            action["review_gate_job_id"] = review_id
        actions.append(action)
    state["submitted"] = submitted
    save_state(cfg, state)
    print(json.dumps({"actions": actions}, indent=2))
    return 0


def parse_github_remote(url: str) -> tuple[str, str] | None:
    cleaned = url.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    patterns = [
        r"github\.com[:/]([^/]+)/([^/]+)$",
        r"github\.com/([^/]+)/([^/]+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if match:
            return match.group(1), match.group(2)
    return None


def git_remote_for_repo(repo_path: Path) -> tuple[str, str] | None:
    root = canonical_repo_path(repo_path)
    proc = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True, check=False, timeout=20)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return parse_github_remote(proc.stdout.strip())


def github_auth_header() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return f"token {token}"
    creds = Path.home() / ".git-credentials"
    if not creds.exists():
        return None
    text = creds.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"https://([^:]+):([^@]+)@github.com", text)
    if not match:
        return None
    user, secret = match.group(1), match.group(2)
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


def github_get(url: str) -> Any:
    auth = github_auth_header()
    if not auth:
        raise RuntimeError("GitHub credentials unavailable")
    req = urllib.request.Request(url)
    req.add_header("Authorization", auth)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "hermes-tasklane")
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else None


def github_post(url: str, payload: dict[str, Any]) -> Any:
    auth = github_auth_header()
    if not auth:
        raise RuntimeError("GitHub credentials unavailable")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", auth)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "hermes-tasklane")
    with urllib.request.urlopen(req, timeout=30) as response:
        response_body = response.read().decode("utf-8")
    return json.loads(response_body) if response_body.strip() else None


def github_graphql(query: str, variables: dict[str, Any]) -> Any:
    auth = github_auth_header()
    if not auth:
        raise RuntimeError("GitHub credentials unavailable")
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request("https://api.github.com/graphql", data=body, method="POST")
    req.add_header("Authorization", auth)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "hermes-tasklane")
    with urllib.request.urlopen(req, timeout=30) as response:
        response_body = response.read().decode("utf-8")
    payload = json.loads(response_body) if response_body.strip() else None
    if isinstance(payload, dict) and payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {payload.get('errors')}")
    return payload


def ci_text_indicates_unavailable(*values: Any) -> bool:
    text = "\n".join(str(value or "") for value in values).lower()
    return any(pattern in text for pattern in CI_UNAVAILABLE_PATTERNS)


def check_runs_for_suite(suite: dict[str, Any]) -> list[dict[str, Any]]:
    url = suite.get("check_runs_url")
    if not url:
        return []
    try:
        payload = github_get(str(url)) or {}
    except Exception:
        return []
    runs = payload.get("check_runs") if isinstance(payload, dict) else []
    if not isinstance(runs, list):
        return []
    rows: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        output = run.get("output") if isinstance(run.get("output"), dict) else {}
        rows.append(
            {
                "name": run.get("name"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "title": output.get("title") if isinstance(output, dict) else None,
                "summary": output.get("summary") if isinstance(output, dict) else None,
                "text": output.get("text") if isinstance(output, dict) else None,
            }
        )
    return rows


def ci_status(owner: str, repo: str, sha: str) -> dict[str, Any]:
    combined = github_get(f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/status") or {}
    suites = github_get(f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-suites") or {}
    lines: list[str] = []
    pending = False
    unavailable = False
    hard_failed = False
    suite_rows: list[dict[str, Any]] = []
    for suite in suites.get("check_suites", []) or []:
        app = ((suite.get("app") or {}).get("slug") or (suite.get("app") or {}).get("name") or "unknown")
        status = suite.get("status")
        conclusion = suite.get("conclusion")
        conclusion_lower = str(conclusion or "").lower()
        suite_row: dict[str, Any] = {"app": app, "status": status, "conclusion": conclusion}
        lines.append(f"suite {app}: {status}/{conclusion or 'pending'}")
        if status != "completed" or not conclusion:
            pending = True
        elif conclusion_lower in CI_FAIL_CONCLUSIONS:
            check_runs = check_runs_for_suite(suite)
            if check_runs:
                suite_row["check_runs"] = [
                    {
                        "name": run.get("name"),
                        "status": run.get("status"),
                        "conclusion": run.get("conclusion"),
                        "title": run.get("title"),
                    }
                    for run in check_runs
                ]
            suite_unavailable = conclusion_lower in CI_UNAVAILABLE_CONCLUSIONS or ci_text_indicates_unavailable(
                app,
                suite.get("name"),
                suite.get("conclusion"),
                suite.get("status"),
            )
            if not suite_unavailable:
                suite_unavailable = any(
                    str(run.get("conclusion") or "").lower() in CI_UNAVAILABLE_CONCLUSIONS
                    or ci_text_indicates_unavailable(run.get("name"), run.get("title"), run.get("summary"), run.get("text"))
                    for run in check_runs
                )
            if suite_unavailable:
                unavailable = True
                lines.append(f"suite {app}: CI unavailable")
            else:
                hard_failed = True
        suite_rows.append(suite_row)
    statuses = combined.get("statuses") if isinstance(combined, dict) else []
    if isinstance(statuses, list):
        for status_item in statuses:
            if not isinstance(status_item, dict):
                continue
            state = str(status_item.get("state") or "").lower()
            if state in {"failure", "error"}:
                if ci_text_indicates_unavailable(status_item.get("context"), status_item.get("description"), status_item.get("target_url")):
                    unavailable = True
                else:
                    hard_failed = True
            elif state == "pending":
                pending = True
    overall = str(combined.get("state") or "").lower()
    if overall in {"failure", "error"} and not unavailable:
        hard_failed = True
    elif overall == "pending":
        pending = True
    status = "fail" if hard_failed else "unavailable" if unavailable else "pending" if pending else "pass"
    return {
        "status": status,
        "output": "\n".join(lines) if lines else "No GitHub status checks reported",
        "combined_state": combined.get("state"),
        "check_suites": suite_rows,
    }


def find_pr(owner: str, repo: str, branch: str) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"head": f"{owner}:{branch}", "state": "all"})
    prs = github_get(f"https://api.github.com/repos/{owner}/{repo}/pulls?{query}")
    if isinstance(prs, list) and prs:
        pr = prs[0]
        return {
            "number": pr.get("number"),
            "url": pr.get("html_url"),
            "title": pr.get("title"),
            "state": pr.get("state"),
            "merged_at": pr.get("merged_at"),
            "head_sha": ((pr.get("head") or {}).get("sha")),
            "branch": ((pr.get("head") or {}).get("ref")),
            "base_branch": ((pr.get("base") or {}).get("ref")),
        }
    return None


def remote_branch_exists(repo_path: Path, branch: str) -> tuple[bool | None, str | None]:
    """Return (exists, error); exists=None means git could not answer."""
    if not branch:
        return False, None
    proc = run_process(["git", "-C", str(repo_path), "ls-remote", "--heads", "origin", branch], timeout=30)
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "git ls-remote failed").strip()
    return bool((proc.stdout or "").strip()), None


def pr_visibility_status(cfg: Config, job: dict[str, Any]) -> dict[str, Any]:
    spec = job.get("spec") or {}
    branch = spec.get("branch") or {}
    repo_path = job_repo_path_from_spec(job)
    work_branch = str(branch.get("work_branch") or "").strip()
    base_branch = str(branch.get("pr_target") or branch.get("base_branch") or "").strip()
    cached = compact_pr_status_from_job(job)
    if cached.get("status") == "found":
        cached.setdefault("head", work_branch or cached.get("head"))
        cached.setdefault("base", base_branch or cached.get("base"))
        return cached
    base = {"status": "not-found", "url": None, "number": None, "head": work_branch or None, "base": base_branch or None, "message": "No PR found for branch"}
    if not repo_path or not work_branch:
        return {**base, "status": "not-found", "message": "Job has no repository path or work branch"}
    remote = git_remote_for_repo(repo_path)
    if not remote:
        exists, error = remote_branch_exists(repo_path, work_branch)
        if exists is True:
            return {**base, "status": "branch-pushed-no-pr", "message": "Remote branch exists; GitHub remote could not be parsed for PR lookup"}
        if exists is None:
            return {**base, "status": "query-failed", "message": error or "Remote branch check failed"}
        return base
    owner, repo = remote
    if not github_auth_header():
        exists, error = remote_branch_exists(repo_path, work_branch)
        status = "unknown-auth-missing"
        message = "GitHub credentials unavailable; PR lookup not attempted"
        if exists is True:
            message += "; remote branch exists"
        elif exists is False:
            message += "; remote branch not found"
        elif error:
            message += f"; remote branch check failed: {error}"
        return {**base, "status": status, "message": message}
    try:
        pr = find_pr(owner, repo, work_branch)
    except RuntimeError as exc:
        if "credentials" in str(exc).lower():
            return {**base, "status": "unknown-auth-missing", "message": str(exc)}
        return {**base, "status": "query-failed", "message": str(exc)}
    except Exception as exc:
        return {**base, "status": "query-failed", "message": str(exc)}
    if pr:
        return {
            "status": "found",
            "url": pr.get("url"),
            "number": pr.get("number"),
            "head": pr.get("branch") or work_branch or None,
            "base": pr.get("base_branch") or base_branch or None,
            "message": "PR found",
        }
    exists, error = remote_branch_exists(repo_path, work_branch)
    if exists is True:
        return {**base, "status": "branch-pushed-no-pr", "message": "Remote branch exists but no PR was found"}
    if exists is None:
        return {**base, "status": "query-failed", "message": error or "Remote branch check failed after PR lookup"}
    return base


def create_pr(owner: str, repo: str, *, branch: str, base_branch: str, title: str, body: str) -> dict[str, Any]:
    pr = github_post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        {
            "title": title,
            "head": branch,
            "base": base_branch,
            "body": body,
            "draft": False,
        },
    )
    return {
        "number": pr.get("number"),
        "url": pr.get("html_url"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "merged_at": pr.get("merged_at"),
        "head_sha": ((pr.get("head") or {}).get("sha")),
        "branch": ((pr.get("head") or {}).get("ref")),
        "base_branch": ((pr.get("base") or {}).get("ref")),
    }


def github_open_pull_requests(owner: str, repo: str) -> list[dict[str, Any]]:
    pulls = github_get(f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page=100") or []
    if not isinstance(pulls, list):
        return []
    rows: list[dict[str, Any]] = []
    for pr in pulls:
        if not isinstance(pr, dict):
            continue
        rows.append(
            {
                "number": pr.get("number"),
                "url": pr.get("html_url"),
                "title": pr.get("title"),
                "body": pr.get("body") or "",
                "state": pr.get("state"),
                "draft": pr.get("draft"),
                "head_branch": ((pr.get("head") or {}).get("ref")),
                "head_sha": ((pr.get("head") or {}).get("sha")),
                "base_branch": ((pr.get("base") or {}).get("ref")),
                "labels": [label.get("name") for label in pr.get("labels", []) if isinstance(label, dict)],
            }
        )
    return rows


def github_pull_request(owner: str, repo: str, number: int) -> dict[str, Any]:
    pr = github_get(f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}") or {}
    return pr if isinstance(pr, dict) else {}


def github_blocking_review_threads(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 100) {
            nodes {
              id
              isResolved
              isOutdated
              path
              line
              comments(first: 20) {
                nodes {
                  body
                  url
                  author { login }
                }
              }
            }
          }
        }
      }
    }
    """
    payload = github_graphql(query, {"owner": owner, "repo": repo, "number": number})
    threads = (((payload or {}).get("data") or {}).get("repository") or {}).get("pullRequest", {}).get("reviewThreads", {}).get("nodes") or []
    blockers: list[dict[str, Any]] = []
    for thread in threads:
        if not isinstance(thread, dict) or thread.get("isResolved") or thread.get("isOutdated"):
            continue
        comments = ((thread.get("comments") or {}).get("nodes") or [])
        blocking_comment = None
        for comment in comments:
            body = str((comment or {}).get("body") or "")
            if re.search(r"\bP[0-2]\b|P[0-2]\s+Badge", body, flags=re.IGNORECASE):
                blocking_comment = comment
                break
        if not blocking_comment:
            continue
        first_line = next((line.strip("#* _") for line in str(blocking_comment.get("body") or "").splitlines() if line.strip()), "blocking review thread")
        blockers.append(
            {
                "id": thread.get("id"),
                "path": thread.get("path"),
                "line": thread.get("line"),
                "url": blocking_comment.get("url"),
                "reason": compact_message_line(first_line),
            }
        )
    return blockers


def pr_body_contains_command(body: str, command: str) -> bool:
    command = command.strip()
    return bool(command and (f"`{command}`" in body or command in body))


def review_text_addresses_blocker(review_text: str, blocker: dict[str, Any]) -> bool:
    text = review_text.lower()
    if not text:
        return False
    resolution_words = ("fixed", "addressed", "resolved", "obsolete", "no longer applies", "no longer reproducible")
    if not any(word in text for word in resolution_words):
        return False
    url = str(blocker.get("url") or "").lower()
    if url and url in text:
        return True
    reason = str(blocker.get("reason") or "").lower()
    words = [word for word in re.findall(r"[a-z0-9]+", reason) if len(word) >= 5]
    return bool(words and sum(1 for word in words[:8] if word in text) >= 3)


def github_pr_gate_findings(root_job: dict[str, Any], *, review_text: str = "") -> list[dict[str, Any]]:
    ref = github_pr_reference_from_job(root_job)
    if not ref:
        return []
    try:
        pr = github_pull_request(str(ref["owner"]), str(ref["repo"]), int(ref["number"]))
    except Exception as exc:
        return [{"code": "github-pr-lookup-failed", "message": f"GitHub PR lookup failed: {exc}"}]
    body = str(pr.get("body") or "")
    head_sha = str(((pr.get("head") or {}).get("sha")) or "")
    findings: list[dict[str, Any]] = []
    if head_sha and head_sha not in body and head_sha[:12] not in body:
        findings.append(
            {
                "code": "pr-body-head-sha-stale",
                "message": f"PR body does not mention current head SHA {head_sha}.",
            }
        )
    changed_files = pr.get("changed_files")
    additions = pr.get("additions")
    deletions = pr.get("deletions")
    if isinstance(changed_files, int) and f"{changed_files} files changed" not in body:
        findings.append({"code": "pr-body-diffstat-stale", "message": f"PR body does not mention current diffstat file count: {changed_files} files changed."})
    if isinstance(additions, int) and str(additions) not in body:
        findings.append({"code": "pr-body-diffstat-stale", "message": f"PR body does not mention current addition count: {additions}."})
    if isinstance(deletions, int) and str(deletions) not in body:
        findings.append({"code": "pr-body-diffstat-stale", "message": f"PR body does not mention current deletion count: {deletions}."})
    metadata = ((root_job.get("spec") or {}).get("metadata") or {})
    for command in parse_csv(str(metadata.get("required_verification_commands") or "")):
        if not pr_body_contains_command(body, command):
            findings.append({"code": "pr-body-missing-verification", "message": f"PR body is missing required verification command `{command}`."})
    try:
        blockers = github_blocking_review_threads(str(ref["owner"]), str(ref["repo"]), int(ref["number"]))
    except Exception as exc:
        findings.append({"code": "github-review-thread-check-failed", "message": f"Could not inspect GitHub review threads: {exc}"})
        blockers = []
    for blocker in blockers:
        if review_text_addresses_blocker(review_text, blocker):
            continue
        findings.append(
            {
                "code": "unresolved-blocking-review-thread",
                "message": f"Unresolved blocking review thread remains: {blocker.get('reason')}",
                "url": blocker.get("url"),
                "path": blocker.get("path"),
                "line": blocker.get("line"),
            }
        )
    return findings


def review_gate_guard_payload(review_job: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    lines = [
        "TASKLANE_REVIEW_DECISION: needs-fix",
        "",
        "Tasklane guard findings:",
    ]
    for index, finding in enumerate(findings, start=1):
        location = ""
        if finding.get("path"):
            location = f" ({finding.get('path')}:{finding.get('line')})" if finding.get("line") else f" ({finding.get('path')})"
        url = f" {finding.get('url')}" if finding.get("url") else ""
        lines.append(f"{index}. {finding.get('message')}{location}{url}")
    lines.extend(
        [
            "",
            "Expected fix:",
            "- Update the PR branch or PR body so Tasklane evidence matches the current PR head.",
            "- Resolve or address unresolved blocking GitHub review threads before merge.",
        ]
    )
    guarded = dict(review_job)
    guarded["result"] = {"final_response": "\n".join(lines)}
    return guarded


def github_open_issues(owner: str, repo: str, *, limit: int) -> list[dict[str, Any]]:
    issues = github_get(f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&sort=created&direction=asc&per_page={min(max(limit, 1), 100)}") or []
    if not isinstance(issues, list):
        return []
    rows: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("pull_request"):
            continue
        rows.append(
            {
                "number": issue.get("number"),
                "url": issue.get("html_url"),
                "title": issue.get("title"),
                "body": issue.get("body") or "",
                "labels": [label.get("name") for label in issue.get("labels", []) if isinstance(label, dict)],
                "milestone": (issue.get("milestone") or {}).get("title") if isinstance(issue.get("milestone"), dict) else None,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def wave_planner_settings(
    cfg: Config,
    *,
    max_active_prs: int | None = None,
    branch_prefix: str | None = None,
    issue_limit: int | None = None,
    issue_scan_limit: int | None = None,
    max_lanes: int | None = None,
    issue_includes: list[str] | None = None,
    issue_excludes: list[str] | None = None,
    issue_labels_any: list[str] | None = None,
    issue_labels_all: list[str] | None = None,
    issue_milestone: str | None = None,
) -> dict[str, Any]:
    raw = cfg.wave_planner or {}
    max_issues_per_wave = int(issue_limit if issue_limit is not None else raw.get("max_issues_per_wave") or 10)
    scan_limit = int(issue_scan_limit if issue_scan_limit is not None else raw.get("issue_scan_limit") or max(max_issues_per_wave * 5, max_issues_per_wave))
    return {
        "max_active_prs": int(max_active_prs if max_active_prs is not None else raw.get("max_active_prs") or 3),
        "branch_prefix": str(branch_prefix if branch_prefix is not None else raw.get("branch_prefix") or "tasklane/"),
        "max_issues_per_wave": max_issues_per_wave,
        "issue_scan_limit": max(scan_limit, max_issues_per_wave),
        "max_pr_lanes": int(max_lanes if max_lanes is not None else raw.get("max_pr_lanes") or 3),
        "review_loops": int(raw.get("review_loops") or 2),
        "max_active_contract_prs": int(raw.get("max_active_contract_prs") or 1),
        "max_active_large_feature_prs": int(raw.get("max_active_large_feature_prs") or 1),
        "max_active_docs_prs": int(raw.get("max_active_docs_prs") or 2),
        "issue_include_terms": string_list(issue_includes if issue_includes is not None else raw.get("issue_include_terms")),
        "issue_exclude_terms": string_list(issue_excludes if issue_excludes is not None else raw.get("issue_exclude_terms")),
        "issue_labels_any": string_list(issue_labels_any if issue_labels_any is not None else raw.get("issue_labels_any")),
        "issue_labels_all": string_list(issue_labels_all if issue_labels_all is not None else raw.get("issue_labels_all")),
        "issue_milestone": str(issue_milestone if issue_milestone is not None else raw.get("issue_milestone") or "").strip(),
    }


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def issue_text(issue: dict[str, Any]) -> str:
    labels = " ".join(str(label or "") for label in issue.get("labels") or [])
    return f"{issue.get('title') or ''}\n{issue.get('body') or ''}\n{labels}\n{issue.get('milestone') or ''}".lower()


def issue_scope_decision(issue: dict[str, Any], settings: dict[str, Any]) -> tuple[bool, str]:
    text = issue_text(issue)
    labels = {str(label or "").lower() for label in issue.get("labels") or []}
    include_terms = [term.lower() for term in settings.get("issue_include_terms") or []]
    exclude_terms = [term.lower() for term in settings.get("issue_exclude_terms") or []]
    labels_any = {label.lower() for label in settings.get("issue_labels_any") or []}
    labels_all = {label.lower() for label in settings.get("issue_labels_all") or []}
    milestone = str(settings.get("issue_milestone") or "").strip().lower()
    issue_number = str(issue.get("number") or "")
    if include_terms and not any(term in text or term == f"#{issue_number}" for term in include_terms):
        return False, "missing-include-term"
    for term in exclude_terms:
        if term in text or term == f"#{issue_number}":
            return False, f"excluded-term:{term}"
    if labels_any and not labels.intersection(labels_any):
        return False, "missing-any-label"
    if labels_all and not labels_all.issubset(labels):
        return False, "missing-required-label"
    if milestone and str(issue.get("milestone") or "").strip().lower() != milestone:
        return False, "milestone-mismatch"
    return True, "in-scope"


def filter_wave_issues(issues: list[dict[str, Any]], settings: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scoped: list[dict[str, Any]] = []
    scoped_out: list[dict[str, Any]] = []
    for issue in issues:
        ok, reason = issue_scope_decision(issue, settings)
        if ok:
            scoped.append(issue)
        else:
            scoped_out.append(
                {
                    "number": issue.get("number"),
                    "url": issue.get("url"),
                    "title": issue.get("title"),
                    "reason": reason,
                }
            )
    return scoped, scoped_out


def extract_issue_paths(issue: dict[str, Any]) -> list[str]:
    text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
    paths: list[str] = []
    for value in re.findall(r"`([^`\n]+)`", text):
        cleaned = value.strip()
        if "/" in cleaned or cleaned.endswith((".ts", ".tsx", ".rs", ".md", ".json")):
            paths.append(cleaned)
    return paths[:12]


def classify_wave_issue(issue: dict[str, Any]) -> dict[str, Any]:
    text = issue_text(issue)
    paths = extract_issue_paths(issue)
    combined = f"{text}\n{chr(10).join(paths).lower()}"
    domains: list[str] = []
    if any(token in combined for token in ["contracts/", "anchor", "idl", "program", "on-chain", "onchain", "pda"]):
        domains.append("contract")
    if any(token in combined for token in ["apps/client", "frontend", "client", "ui", "component", "panel", "modal", "ux"]):
        domains.append("frontend")
    if any(token in combined for token in ["apps/server", "server", "backend", "api", "gateway", "quest.service", "handler", "database", "db/", "src/lib/db"]):
        domains.append("backend")
    if any(token in combined for token in ["telemetry", "metrics", "event", "log"]):
        domains.append("telemetry")
    if any(token in combined for token in ["balance", "economy", "recipe", "drop", "crafting", "cost"]):
        domains.append("economy")
    if any(token in combined for token in ["docs/", "checklist", "rollout", "readiness", "qa guide"]):
        domains.append("docs")
    if any(token in combined for token in ["e2e", "testnet", "campaign", "playwright"]):
        domains.append("e2e")
    if not domains:
        domains.append("general")
    risk_flags: list[str] = []
    if "contract" in domains:
        risk_flags.append("contract")
    if any(token in combined for token in ["large", "full onboarding", "campaign", "dungeon run", "marketplace", "1-2 days"]):
        risk_flags.append("large-feature")
    if any(token in combined for token in ["deploy", "upgrade", "migration", "secret", "api key", "supabase"]):
        risk_flags.append("blocker-prone")
    if "contract" in domains:
        lane_key = "contract"
    elif any(domain in domains for domain in ["telemetry", "economy", "docs"]):
        lane_key = "ops-readiness"
    elif "frontend" in domains and "backend" not in domains:
        lane_key = "ux"
    elif "backend" in domains:
        lane_key = "backend"
    elif "e2e" in domains:
        lane_key = "e2e"
    else:
        lane_key = "general"
    return {
        "number": issue.get("number"),
        "url": issue.get("url"),
        "title": issue.get("title"),
        "domains": domains,
        "risk_flags": risk_flags,
        "paths": paths,
        "lane_key": lane_key,
    }


def tasklane_active_prs(open_prs: list[dict[str, Any]], branch_prefix: str) -> list[dict[str, Any]]:
    return [pr for pr in open_prs if str(pr.get("head_branch") or "").startswith(branch_prefix)]


def referenced_issue_numbers(prs: list[dict[str, Any]]) -> set[int]:
    numbers: set[int] = set()
    for pr in prs:
        text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}\n{pr.get('head_branch') or ''}"
        for value in re.findall(r"#(\d+)", text):
            numbers.add(int(value))
    return numbers


def issue_reference_terms(issue: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    number = issue.get("number")
    if isinstance(number, int):
        terms.append(str(number))
        terms.append(f"#{number}")
    title = str(issue.get("title") or "")
    text = f"{title}\n{issue.get('body') or ''}"
    for value in re.findall(r"\bS\d+-T\d+\b", text, flags=re.IGNORECASE):
        normalized = value.upper()
        if normalized not in terms:
            terms.append(normalized)
    title_phrase = re.sub(r"^\[[^\]]+\]\s*", "", title)
    title_phrase = re.sub(r"^\d+\s*[-:]\s*", "", title_phrase).strip()
    title_words = re.findall(r"[A-Za-z0-9]+", title_phrase)
    if len(title_words) >= 2:
        phrase = " ".join(title_words[:6])
        if phrase not in terms:
            terms.append(phrase)
    return terms


def github_merged_prs_for_issue(owner: str, repo: str, issue: dict[str, Any]) -> list[dict[str, Any]]:
    terms = issue_reference_terms(issue)
    if not terms:
        return []
    number = issue.get("number")
    seen: set[int] = set()
    matches: list[dict[str, Any]] = []
    for term in terms:
        query_term = f'"{term}"' if " " in term or "-" in term or term.startswith("#") else term
        query = f"repo:{owner}/{repo} is:pr is:merged {query_term}"
        url = "https://api.github.com/search/issues?" + urllib.parse.urlencode({"q": query, "per_page": 10})
        try:
            payload = github_get(url) or {}
        except Exception:
            continue
        for item in payload.get("items") or []:
            if not isinstance(item, dict) or not isinstance(item.get("number"), int):
                continue
            pr_number = int(item["number"])
            if pr_number in seen:
                continue
            try:
                pr = github_pull_request(owner, repo, pr_number)
            except Exception:
                continue
            if not pr.get("merged_at"):
                continue
            text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}\n{((pr.get('head') or {}).get('ref')) or ''}".lower()
            normalized_terms = [value.lower() for value in terms]
            if isinstance(number, int) and f"#{number}" in text:
                pass
            elif not any(value in text for value in normalized_terms if not value.isdigit() and not value.startswith("#")):
                continue
            seen.add(pr_number)
            matches.append(
                {
                    "number": pr_number,
                    "url": pr.get("html_url") or item.get("html_url"),
                    "title": pr.get("title") or item.get("title"),
                    "merged_at": pr.get("merged_at"),
                    "matched_term": term,
                }
            )
    return matches


def blocker_payload(project: str, code: str, blocked_item: str, reason: str, action: str, links: list[str] | None = None) -> dict[str, Any]:
    return {
        "severity": "blocker",
        "project": project,
        "blocked_item": blocked_item,
        "reason": reason,
        "required_user_action": action,
        "safe_secret_names_only": True,
        "links": links or [],
    }


def lane_title(lane_key: str) -> str:
    names = {
        "contract": "Contract/IDL Lane",
        "ops-readiness": "Ops Readiness Lane",
        "ux": "UX Gates Lane",
        "backend": "Backend Lane",
        "e2e": "E2E Lane",
        "general": "General Lane",
    }
    return names.get(lane_key, f"{lane_key.replace('-', ' ').title()} Lane")


def build_wave_lanes(candidates: list[dict[str, Any]], *, project: str, branch_prefix: str, max_lanes: int, review_loops: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lanes_by_key: dict[str, dict[str, Any]] = {}
    blocked: list[dict[str, Any]] = []
    for candidate in candidates:
        if "large-feature" in candidate["risk_flags"] and candidate["lane_key"] in {"e2e", "general"}:
            blocked.append({"issue": candidate, "reason": "issue-too-risky-for-overnight", "message": "large or broad issue should be planned manually"})
            continue
        lane_key = str(candidate["lane_key"])
        if lane_key == "contract" and lane_key in lanes_by_key:
            blocked.append({"issue": candidate, "reason": "contract-pr-cap", "message": "only one contract/IDL issue is allowed per dry-run wave"})
            continue
        if lane_key not in lanes_by_key:
            if len(lanes_by_key) >= max_lanes:
                blocked.append({"issue": candidate, "reason": "lane-cap", "message": f"max_pr_lanes={max_lanes} already reached"})
                continue
            lanes_by_key[lane_key] = {
                "lane_id": lane_key,
                "title": lane_title(lane_key),
                "branch": f"{branch_prefix}{lane_key}",
                "issues": [],
                "grouping_reason": "same domain/file ownership; serial implementation avoids conflicts",
                "review_gate": {"enabled": True, "review_loops": review_loops},
            }
        lanes_by_key[lane_key]["issues"].append(candidate)
    lanes = list(lanes_by_key.values())
    for lane in lanes:
        implementation_tasks: list[dict[str, Any]] = []
        previous_id: str | None = None
        dependency_ids: list[str] = []
        for issue in lane["issues"]:
            task_id = f"issue-{issue['number']}"
            implementation_tasks.append(
                {
                    "task_id": task_id,
                    "issue": issue["number"],
                    "delivery_mode": "direct-push",
                    "depends_on": [previous_id] if previous_id else [],
                    "serial_reason": "shared lane ownership/conflict guard" if previous_id else "lane root task",
                }
            )
            dependency_ids.append(task_id)
            previous_id = task_id
        lane["implementation_tasks"] = implementation_tasks
        lane["final_pr_task"] = {
            "task_id": f"{lane['lane_id']}-final-pr",
            "delivery_mode": "pull-request",
            "depends_on": dependency_ids,
            "codex_review": True,
            "review_loops": review_loops,
        }
        lane["project"] = project
    return lanes, blocked


def current_job_summary(cfg: Config) -> dict[str, Any]:
    completed = completed_job_ids(cfg)
    ready, waiting, waiting_for = split_ready_jobs(cfg)
    return {
        "running": [compact_job(job, completed_ids=completed) for job in iter_job_records(cfg, {"running"})],
        "ready": [compact_job(job, completed_ids=completed) for job in ready],
        "waiting": [compact_job(job, state="waiting", waiting_for=waiting_for.get(str(job.get("id") or ""), []), completed_ids=completed) for job in waiting],
        "blocked": [compact_job(job, completed_ids=completed) for job in iter_job_records(cfg, {"blocked", "needs-human"})],
        "failed": [compact_job(job, completed_ids=completed) for job in iter_job_records(cfg, {"failed"})],
        "completed_count": len(completed),
    }


def plan_wave_report(
    cfg: Config,
    *,
    repo_path: Path,
    project: str,
    base_branch: str,
    max_active_prs: int | None = None,
    branch_prefix: str | None = None,
    issue_limit: int | None = None,
    issue_scan_limit: int | None = None,
    max_lanes: int | None = None,
    issue_includes: list[str] | None = None,
    issue_excludes: list[str] | None = None,
    issue_labels_any: list[str] | None = None,
    issue_labels_all: list[str] | None = None,
    issue_milestone: str | None = None,
) -> dict[str, Any]:
    ensure_layout(cfg)
    settings = wave_planner_settings(
        cfg,
        max_active_prs=max_active_prs,
        branch_prefix=branch_prefix,
        issue_limit=issue_limit,
        issue_scan_limit=issue_scan_limit,
        max_lanes=max_lanes,
        issue_includes=issue_includes,
        issue_excludes=issue_excludes,
        issue_labels_any=issue_labels_any,
        issue_labels_all=issue_labels_all,
        issue_milestone=issue_milestone,
    )
    project_settings = project_wave_settings(cfg, project)
    project_override_map = {
        "max_active_prs": max_active_prs,
        "branch_prefix": branch_prefix,
        "max_issues_per_wave": issue_limit,
        "issue_scan_limit": issue_scan_limit,
        "max_pr_lanes": max_lanes,
        "issue_include_terms": issue_includes,
        "issue_exclude_terms": issue_excludes,
        "issue_labels_any": issue_labels_any,
        "issue_labels_all": issue_labels_all,
        "issue_milestone": issue_milestone,
    }
    for key, explicit_value in project_override_map.items():
        if explicit_value is None and key in project_settings:
            if key in {"issue_include_terms", "issue_exclude_terms", "issue_labels_any", "issue_labels_all"}:
                settings[key] = string_list(project_settings.get(key))
            elif key in {"branch_prefix", "issue_milestone"}:
                settings[key] = str(project_settings.get(key) or settings[key])
            else:
                settings[key] = int(project_settings.get(key) or settings[key])
    remote = git_remote_for_repo(repo_path)
    if not remote:
        raise RuntimeError(f"GitHub remote unavailable for {repo_path}")
    owner, repo = remote
    open_prs = github_open_pull_requests(owner, repo)
    active_prs = tasklane_active_prs(open_prs, settings["branch_prefix"])
    active_issue_numbers = referenced_issue_numbers(active_prs)
    active_contract_prs = [pr for pr in active_prs if "contract" in str(pr.get("title") or "").lower() or "idl" in str(pr.get("title") or "").lower()]
    issues = github_open_issues(owner, repo, limit=settings["issue_scan_limit"])
    scoped_issues, scoped_out_items = filter_wave_issues(issues, settings)
    already_covered_items: list[dict[str, Any]] = []
    available_issues: list[dict[str, Any]] = []
    for issue in scoped_issues:
        number = issue.get("number")
        if isinstance(number, int) and number in active_issue_numbers:
            already_covered_items.append(
                {
                    "number": issue.get("number"),
                    "url": issue.get("url"),
                    "title": issue.get("title"),
                    "reason": "active-tasklane-pr",
                }
            )
            continue
        merged_prs = github_merged_prs_for_issue(owner, repo, issue)
        if merged_prs:
            already_covered_items.append(
                {
                    "number": issue.get("number"),
                    "url": issue.get("url"),
                    "title": issue.get("title"),
                    "reason": "merged-pr-reference",
                    "merged_prs": merged_prs[:5],
                }
            )
            continue
        else:
            available_issues.append(issue)
    selected_issues = available_issues[: settings["max_issues_per_wave"]]
    deferred_items = [
        {
            "number": issue.get("number"),
            "url": issue.get("url"),
            "title": issue.get("title"),
            "reason": "wave-issue-limit",
        }
        for issue in available_issues[settings["max_issues_per_wave"] :]
    ]
    candidates = [classify_wave_issue(issue) for issue in selected_issues]
    if bool_from_any(project_settings.get("disable_contract_lane"), default=False):
        for candidate in candidates:
            if candidate.get("lane_key") == "contract":
                candidate["risk_flags"] = [flag for flag in candidate.get("risk_flags") or [] if flag != "contract"]
                candidate["domains"] = [domain for domain in candidate.get("domains") or [] if domain != "contract"] or ["backend"]
                candidate["lane_key"] = "backend" if "backend" in candidate["domains"] else "ops-readiness"
    blocked_items: list[dict[str, Any]] = []
    notifications: list[dict[str, Any]] = []
    mode = "plan-new-work"
    remaining_pr_slots = max(settings["max_active_prs"] - len(active_prs), 0)
    may_start = remaining_pr_slots > 0
    lane_limit = min(settings["max_pr_lanes"], remaining_pr_slots)
    lanes: list[dict[str, Any]] = []
    lane_blockers: list[dict[str, Any]] = []
    if not may_start:
        mode = "review-fix-unblock"
        payload = blocker_payload(
            project,
            "active-pr-cap",
            f"{len(active_prs)} active tasklane PRs",
            f"max_active_prs={settings['max_active_prs']} reached",
            "Review, fix, or merge existing tasklane PRs before starting new implementation.",
            [str(pr.get("url")) for pr in active_prs if pr.get("url")],
        )
        blocked_items.append(payload)
        notifications.append(payload)
    elif len(active_contract_prs) >= settings["max_active_contract_prs"]:
        payload = blocker_payload(
            project,
            "contract-pr-cap",
            f"{len(active_contract_prs)} active contract tasklane PRs",
            f"max_active_contract_prs={settings['max_active_contract_prs']} reached",
            "Do not start another contract/IDL lane until the active contract PR is resolved.",
            [str(pr.get("url")) for pr in active_contract_prs if pr.get("url")],
        )
        blocked_items.append(payload)
        notifications.append(payload)
        lanes, lane_blockers = build_wave_lanes([candidate for candidate in candidates if candidate["lane_key"] != "contract"], project=project, branch_prefix=settings["branch_prefix"], max_lanes=lane_limit, review_loops=settings["review_loops"])
    else:
        lanes, lane_blockers = build_wave_lanes(candidates, project=project, branch_prefix=settings["branch_prefix"], max_lanes=lane_limit, review_loops=settings["review_loops"])
    blocked_items.extend(lane_blockers)
    review_attention = [
        {
            "pr": pr,
            "attention": "review/fix/merge candidate",
            "merge_dry_run": "not-evaluated",
            "required_evidence": ["CI green", "local verify green", "Codex review pass", "branch up to date", "no contract upgrade required"],
        }
        for pr in active_prs
    ]
    return {
        "dry_run": True,
        "project": project,
        "repo": {"owner": owner, "name": repo, "path": str(repo_path), "base_branch": base_branch},
        "settings": settings,
        "mode": mode,
        "may_start_new_work": may_start,
        "remaining_pr_slots": remaining_pr_slots,
        "active_tasklane_prs": active_prs,
        "current_jobs": current_job_summary(cfg),
        "issue_scope": {
            "include_terms": settings["issue_include_terms"],
            "exclude_terms": settings["issue_exclude_terms"],
            "labels_any": settings["issue_labels_any"],
            "labels_all": settings["issue_labels_all"],
            "milestone": settings["issue_milestone"],
            "scanned_count": len(issues),
            "matched_count": len(scoped_issues),
            "already_covered_count": len(already_covered_items),
            "selected_count": len(selected_issues),
        },
        "issue_candidates": candidates,
        "scoped_out_items": scoped_out_items,
        "already_covered_items": already_covered_items,
        "deferred_items": deferred_items,
        "proposed_lanes": lanes if may_start else [],
        "blocked_items": blocked_items,
        "notification_payloads": notifications,
        "prs_needing_attention": review_attention,
    }


def format_plan_wave_report(report: dict[str, Any]) -> str:
    lines = [
        "Tasklane Wave Plan",
        f"Project: {report.get('project')}",
        f"Mode: {report.get('mode')}",
        f"May start new work: {str(report.get('may_start_new_work')).lower()}",
        f"Active tasklane PRs: {len(report.get('active_tasklane_prs') or [])}/{(report.get('settings') or {}).get('max_active_prs')}",
    ]
    scope = report.get("issue_scope") or {}
    if scope:
        include_terms = ", ".join(scope.get("include_terms") or []) or "none"
        labels_any = ", ".join(scope.get("labels_any") or []) or "none"
        milestone = scope.get("milestone") or "none"
        lines.append(f"Issue scope: include={include_terms}; labels_any={labels_any}; milestone={milestone}")
        lines.append(
            f"Issues: scanned={scope.get('scanned_count')}; matched={scope.get('matched_count')}; "
            f"already-covered={scope.get('already_covered_count')}; selected={scope.get('selected_count')}"
        )
    active_prs = report.get("active_tasklane_prs") or []
    if active_prs:
        lines.append("")
        lines.append("Active PRs:")
        for pr in active_prs:
            lines.append(f"- #{pr.get('number')} {pr.get('head_branch')}: {pr.get('title')}")
    lanes = report.get("proposed_lanes") or []
    if lanes:
        lines.append("")
        lines.append("Proposed lanes:")
        for lane in lanes:
            issue_numbers = ", ".join(f"#{issue.get('number')}" for issue in lane.get("issues") or [])
            lines.append(f"- {lane.get('title')} -> {lane.get('branch')} ({issue_numbers})")
    blockers = report.get("blocked_items") or []
    if blockers:
        lines.append("")
        lines.append("Blocked/skipped:")
        for item in blockers:
            if "issue" in item:
                issue = item.get("issue") or {}
                lines.append(f"- #{issue.get('number')}: {item.get('reason')} - {item.get('message')}")
            else:
                lines.append(f"- {item.get('blocked_item')}: {item.get('reason')}")
    return "\n".join(lines)


def task_frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, list):
            rendered = ", ".join(str(item) for item in value if str(item))
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines)


def issue_allowed_paths(issue: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for path in issue.get("paths") or []:
        text = str(path).strip()
        if not text or text.startswith("/") or ":" in text:
            continue
        paths.append(text)
    return paths[:16]


def implementation_prompt(issue: dict[str, Any], *, branch: str, review_docs: list[str] | None = None) -> str:
    return f"""Implement only GitHub issue #{issue.get('number')}: {issue.get('title')}.

Use the existing repo instructions. Read AGENTS.md first, then:
{format_review_docs(review_docs or [])}

Delivery rules:
- Work on branch `{branch}`.
- Use direct-push only for this implementation task.
- Do not open a PR from this task.
- Do not merge, deploy, or upgrade contracts.
- Keep scope to the issue and document any follow-up work instead of coding unrelated tickets.
- Run the strongest relevant verification for touched areas.
- If blocked by secrets, deployed contract upgrade, missing config, or an unsafe dependency, stop and report the exact blocker and required user action.
"""


def final_pr_prompt(lane: dict[str, Any], *, branch: str, base_branch: str, review_docs: list[str] | None = None, required_verification: list[str] | None = None) -> str:
    issues = ", ".join(f"#{issue.get('number')}" for issue in lane.get("issues") or [])
    return f"""Finalize the grouped Tasklane PR for issues {issues}.

Use branch `{branch}` targeting `{base_branch}`.

Mandatory context:
{format_review_docs(review_docs or [])}

Required verification:
{format_required_verification(required_verification or [])}

Required:
- Review all implementation commits on this lane branch.
- Resolve integration issues without expanding scope.
- Open or update one PR for this lane.
- Mark the PR ready for review when local verification and PR evidence are complete; leave it draft only if a concrete blocker remains, and document that blocker.
- Add a PR body with implemented issues, changed files, diffstat, exact verification commands/results, residual risks, and intentionally deferred follow-ups.
- Use GitHub closing keywords such as `Closes #123` for every issue that is fully implemented by this PR; mention deferred or partial issues without closing keywords so they stay open.
- After every fix commit, refresh the PR body so the head SHA, diffstat, changed files, verification commands, and test counts match the current PR head exactly.
- Run the required verification before reporting done. If any command cannot run or fails, fix the issue in scope or stop with an explicit blocker.
- Enable the Codex review gate; if review needs fixes, fix on the same branch and re-review within the configured loop cap.

Do not merge, deploy, or upgrade contracts.
"""


def lane_plan_artifact_path(cfg: Config, wave_id: str) -> Path:
    return cfg.task_root / "lane-plans" / f"{wave_id}.json"


def lane_plan_lookup(cfg: Config, job_id: str) -> dict[str, Any] | None:
    directory = cfg.task_root / "lane-plans"
    if not directory.exists():
        return None
    for path in sorted(directory.glob("*.json")):
        payload = load_json(path)
        if not isinstance(payload, dict):
            continue
        for lane in payload.get("lanes") or []:
            if not isinstance(lane, dict):
                continue
            if job_id in {str(item) for item in lane.get("job_ids") or []}:
                return {
                    "wave_id": payload.get("wave_id"),
                    "path": str(path),
                    "artifact_status": payload.get("artifact_status"),
                    "lane": lane,
                }
    return None


def write_lane_plan_artifact(
    cfg: Config,
    *,
    wave_id: str,
    project: str,
    repo_path: Path,
    base_branch: str,
    lanes: list[dict[str, Any]],
) -> dict[str, Any]:
    path = lane_plan_artifact_path(cfg, wave_id)
    try:
        state = load_state(cfg)
        submitted = state.get("submitted") if isinstance(state.get("submitted"), dict) else {}
        artifact_lanes: list[dict[str, Any]] = []
        partial = False
        for lane in lanes:
            task_uids = [str(uid) for uid in lane.get("task_uids") or []]
            impl_uids = [str(uid) for uid in lane.get("implementation_task_uids") or []]
            final_uid = str(lane.get("final_pr_task_uid") or "")
            def jid(uid: str) -> str | None:
                entry = submitted.get(uid) if isinstance(submitted, dict) else None
                if isinstance(entry, dict):
                    return str(entry.get("job_id") or entry.get("run_id") or "").strip() or None
                return None
            job_ids = [jid(uid) for uid in task_uids]
            impl_job_ids = [jid(uid) for uid in impl_uids]
            final_job_id = jid(final_uid) if final_uid else None
            if any(value is None for value in job_ids + impl_job_ids) or (final_uid and final_job_id is None):
                partial = True
            artifact_lanes.append(
                {
                    "delivery_group": lane.get("delivery_group"),
                    "lane_id": lane.get("lane_id"),
                    "branch": lane.get("branch"),
                    "issue_numbers": lane.get("issue_numbers") or [],
                    "task_uids": task_uids,
                    "job_ids": [value for value in job_ids if value],
                    "implementation_task_uids": impl_uids,
                    "implementation_job_ids": [value for value in impl_job_ids if value],
                    "final_pr_task_uid": final_uid or None,
                    "final_pr_job_id": final_job_id,
                }
            )
        payload = {
            "schema_version": 1,
            "wave_id": wave_id,
            "project": project,
            "repo_path": str(repo_path),
            "base_branch": base_branch,
            "created_at": now_iso(),
            "artifact_status": "partial" if partial else "complete",
            "lanes": artifact_lanes,
        }
        atomic_write_json(path, payload)
        return {"status": "partial" if partial else "written", "path": str(path), "error": None}
    except Exception as exc:
        return {"status": "failed", "path": str(path), "error": str(exc)}


def enqueue_wave_tasks(cfg: Config, report: dict[str, Any], *, repo_path: Path, project: str, base_branch: str) -> dict[str, Any]:
    ensure_layout(cfg)
    lanes = report.get("proposed_lanes") or []
    if not report.get("may_start_new_work"):
        return {"status": "blocked", "reason": "may_start_new_work=false", "created_task_files": [], "sync": None}
    if not lanes:
        return {"status": "blocked", "reason": "no-proposed-lanes", "created_task_files": [], "sync": None}
    existing_inbox = sorted(path.name for path in cfg.inbox_dir.iterdir() if path.is_file() and path.suffix.lower() in TASK_SUFFIXES)
    if existing_inbox:
        return {"status": "blocked", "reason": "inbox-not-empty", "existing_inbox": existing_inbox, "created_task_files": [], "sync": None}

    wave_id = datetime.now(timezone.utc).strftime("wave-%Y%m%d%H%M%S")
    project_settings = project_wave_settings(cfg, project)
    review_docs = project_review_docs(cfg, project)
    merge_gate = bool_from_any(project_settings.get("merge_gate"), default=bool_from_any((cfg.wave_planner or {}).get("merge_gate"), default=False))
    auto_merge = bool_from_any(project_settings.get("auto_merge"), default=bool_from_any((cfg.wave_planner or {}).get("auto_merge"), default=False))
    bootstrap_profile = str(project_settings.get("bootstrap_profile") or "").strip()
    verification_profile = str(project_settings.get("verification_profile") or "").strip()
    required_verification = project_verification_commands(cfg, project, verification_profile)
    created: list[str] = []
    lane_plan_lanes: list[dict[str, Any]] = []
    for lane in lanes:
        lane_id = slugify(str(lane.get("lane_id") or "lane"))
        branch = f"{(report.get('settings') or {}).get('branch_prefix') or 'tasklane/'}{slugify(project)}-{wave_id}-{lane_id}"
        issue_uid_by_number: dict[int, str] = {}
        previous_uid: str | None = None
        issues = lane.get("issues") or []
        lane_plan_entry = {
            "delivery_group": f"{wave_id}-{lane_id}",
            "lane_id": lane_id,
            "branch": branch,
            "issue_numbers": [int(issue.get("number")) for issue in issues if issue.get("number") is not None],
            "task_uids": [],
            "implementation_task_uids": [],
            "final_pr_task_uid": None,
        }
        for index, issue in enumerate(issues, start=1):
            number = int(issue.get("number"))
            uid = f"{wave_id}-{lane_id}-issue-{number}"
            issue_uid_by_number[number] = uid
            fields = {
                "id": uid,
                "title": f"{project} {lane.get('title')} #{number}",
                "repo_path": str(repo_path),
                "branch_base": base_branch,
                "branch_mode": "new-branch" if index == 1 else "existing-branch",
                "work_branch": branch,
                "delivery_mode": "direct-push",
                "request_type": "task-small",
                "delivery_group": f"{wave_id}-{lane_id}",
                "project": project,
                "depends_on": previous_uid,
                "allowed_paths": issue_allowed_paths(issue),
                "allow_unlisted_paths": True,
                "review_loops": (lane.get("review_gate") or {}).get("review_loops") or 2,
                "codex_review": False,
                "github_issue": number,
                "review_docs": review_docs,
                "bootstrap_profile": bootstrap_profile,
                "verification_profile": verification_profile,
                "required_verification_commands": required_verification,
            }
            path = cfg.inbox_dir / f"{uid}.md"
            path.write_text(f"{task_frontmatter(fields)}\n{implementation_prompt(issue, branch=branch, review_docs=review_docs)}", encoding="utf-8")
            created.append(str(path))
            lane_plan_entry["task_uids"].append(uid)
            lane_plan_entry["implementation_task_uids"].append(uid)
            previous_uid = uid
        final_uid = f"{wave_id}-{lane_id}-final-pr"
        final_fields = {
            "id": final_uid,
            "title": f"{project} {lane.get('title')} Final PR",
            "repo_path": str(repo_path),
            "branch_base": base_branch,
            "branch_mode": "existing-branch",
            "work_branch": branch,
            "delivery_mode": "pull-request",
            "request_type": "task-small",
            "delivery_group": f"{wave_id}-{lane_id}",
            "project": project,
            "depends_on": list(issue_uid_by_number.values()),
            "allow_unlisted_paths": True,
            "review_loops": (lane.get("review_gate") or {}).get("review_loops") or 2,
            "codex_review": True,
            "merge_gate": merge_gate,
            "auto_merge": auto_merge,
            "review_docs": review_docs,
            "bootstrap_profile": bootstrap_profile,
            "verification_profile": verification_profile,
            "required_verification_commands": required_verification,
        }
        final_path = cfg.inbox_dir / f"{final_uid}.md"
        final_path.write_text(f"{task_frontmatter(final_fields)}\n{final_pr_prompt(lane, branch=branch, base_branch=base_branch, review_docs=review_docs, required_verification=required_verification)}", encoding="utf-8")
        created.append(str(final_path))
        lane_plan_entry["task_uids"].append(final_uid)
        lane_plan_entry["final_pr_task_uid"] = final_uid
        lane_plan_lanes.append(lane_plan_entry)

    sync_output = io.StringIO()
    with redirect_stdout(sync_output):
        command_sync(cfg)
    raw_sync = sync_output.getvalue().strip()
    try:
        sync_payload: Any = json.loads(raw_sync) if raw_sync else None
    except json.JSONDecodeError:
        sync_payload = {"raw": raw_sync}
    lane_plan = {"status": "skipped", "path": None, "error": None}
    if lane_plan_lanes:
        lane_plan = write_lane_plan_artifact(
            cfg,
            wave_id=wave_id,
            project=project,
            repo_path=repo_path,
            base_branch=base_branch,
            lanes=lane_plan_lanes,
        )
    return {
        "status": "enqueued",
        "wave_id": wave_id,
        "created_task_files": created,
        "sync": sync_payload,
        "lane_plan": lane_plan,
    }


def command_plan_wave(
    cfg: Config,
    *,
    repo_path: Path,
    project: str,
    base_branch: str,
    max_active_prs: int | None,
    branch_prefix: str | None,
    issue_limit: int | None,
    issue_scan_limit: int | None,
    max_lanes: int | None,
    issue_includes: list[str] | None,
    issue_excludes: list[str] | None,
    issue_labels_any: list[str] | None,
    issue_labels_all: list[str] | None,
    issue_milestone: str | None,
    enqueue: bool,
    json_output: bool,
) -> int:
    report = plan_wave_report(
        cfg,
        repo_path=repo_path,
        project=project,
        base_branch=base_branch,
        max_active_prs=max_active_prs,
        branch_prefix=branch_prefix,
        issue_limit=issue_limit,
        issue_scan_limit=issue_scan_limit,
        max_lanes=max_lanes,
        issue_includes=issue_includes,
        issue_excludes=issue_excludes,
        issue_labels_any=issue_labels_any,
        issue_labels_all=issue_labels_all,
        issue_milestone=issue_milestone,
    )
    enqueue_result = None
    if enqueue:
        enqueue_result = enqueue_wave_tasks(cfg, report, repo_path=repo_path, project=project, base_branch=base_branch)
        report["enqueue_result"] = enqueue_result
    if json_output:
        print(json.dumps(report, indent=2))
    else:
        print(format_plan_wave_report(report))
        if enqueue_result:
            print("")
            print("Enqueue result:")
            print(json.dumps(enqueue_result, indent=2))
    return 0


def capture_command_json(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    output = io.StringIO()
    with redirect_stdout(output):
        rc = fn(*args, **kwargs)
    text = output.getvalue().strip()
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        payload = {"raw": text}
    payload["_returncode"] = rc
    return payload


def command_wave_runner(
    cfg: Config,
    *,
    repo_path: Path,
    project: str,
    base_branch: str,
    enqueue: bool,
    notify: bool,
    json_output: bool,
) -> int:
    ensure_layout(cfg)
    reconcile = capture_command_json(command_reconcile, cfg)
    watch_report = build_watch_report(
        cfg,
        mode="guarded",
        stale_running_minutes=None,
        expected_base=watch_expected_base_map(cfg),
        ignored_blocked=watch_ignored_blocked_jobs(cfg),
        check_gateway=True,
    )
    apply_guarded_watch_actions(cfg, watch_report)
    watch_report = {
        **build_watch_report(
            cfg,
            mode="guarded",
            stale_running_minutes=None,
            expected_base=watch_expected_base_map(cfg),
            ignored_blocked=watch_ignored_blocked_jobs(cfg),
            check_gateway=True,
        ),
        "actions": watch_report.get("actions") or [],
    }
    plan = plan_wave_report(cfg, repo_path=repo_path, project=project, base_branch=base_branch)
    blockers = list(plan.get("notification_payloads") or [])
    target_repo_key = repo_key(repo_path)
    project_gate_attention = [
        item for item in watch_report.get("gate_attention") or []
        if item.get("repo_key") == target_repo_key
    ]
    project_gate_job_ids = {str(item.get("job_id") or "") for item in project_gate_attention}
    project_problems = [
        problem for problem in watch_report.get("problems") or []
        if ((problem.get("job") or {}).get("repo_key") == target_repo_key)
        or (str(((problem.get("job") or {}).get("id")) or "") in project_gate_job_ids)
    ]
    gateway = watch_report.get("gateway") or {}
    gateway_ok = bool(gateway.get("ok")) or str(gateway.get("state") or "").lower() in {"ok", "active"}
    project_health_ok = gateway_ok and not project_gate_attention and not project_problems
    enqueue_result = None
    if enqueue and project_health_ok and plan.get("may_start_new_work") and plan.get("proposed_lanes"):
        enqueue_result = enqueue_wave_tasks(cfg, plan, repo_path=repo_path, project=project, base_branch=base_branch)
    elif enqueue:
        enqueue_result = {
            "status": "skipped",
            "reason": "project-watch-not-ok-or-no-lanes",
            "watch_health": watch_report.get("health"),
            "project_health_ok": project_health_ok,
            "may_start_new_work": plan.get("may_start_new_work"),
            "proposed_lanes": len(plan.get("proposed_lanes") or []),
        }
    if not project_health_ok:
        if not gateway_ok:
            blockers.append(
                blocker_payload(
                    project,
                    "gateway-not-ok",
                    "tasklane-gateway",
                    "Hermes Tasklane gateway is not healthy.",
                    "Restart or inspect the Hermes Tasklane gateway before starting new work.",
                    [],
                )
            )
        blockers.extend(
            blocker_payload(
                project,
                str(problem.get("code") or "watch-problem"),
                str(((problem.get("job") or {}).get("id")) or "tasklane-watch"),
                str(problem.get("message") or "Tasklane watch found a problem"),
                "Inspect the linked job/PR and decide whether to fix, retry, or stop the lane.",
                [],
            )
            for problem in project_problems
            if problem.get("severity") in {"critical", "warning"}
        )
    report = {
        "project": project,
        "repo": str(repo_path),
        "base_branch": base_branch,
        "rolling": True,
        "reconcile": reconcile,
        "watch": watch_report,
        "plan": plan,
        "enqueue_result": enqueue_result,
        "notifications": blockers,
    }
    if notify and blockers:
        text = "\n".join(
            [
                f"Tasklane {project}: action needed",
                *[f"- {item.get('blocked_item')}: {item.get('reason')}" for item in blockers[:8]],
            ]
        )
        try:
            report["notification"] = maybe_send_tasklane_notification(
                cfg,
                channel=f"wave-runner:{project}",
                text=text,
                payload={"project": project, "blockers": blockers},
            )
        except Exception as exc:
            report["notification"] = {"status": "failed", "error": str(exc)}
    if json_output:
        print(json.dumps(report, indent=2))
    else:
        print(format_plan_wave_report(plan))
        if enqueue_result:
            print("")
            print("Wave runner enqueue:")
            print(json.dumps(enqueue_result, indent=2))
        if blockers:
            print("")
            print("Action needed:")
            for item in blockers[:8]:
                print(f"- {item.get('blocked_item')}: {item.get('reason')}")
    return 0 if not blockers else 2


def update_workflow_stage(run_payload: dict[str, Any], stage: str, message: str) -> None:
    workflow = dict(run_payload.get("workflow") or {})
    history = list(workflow.get("stage_history") or [])
    history.append({
        "stage": stage,
        "timestamp": now_iso(),
        "message": message,
        "issue_id": workflow.get("issue_id"),
        "metadata": {"reconciled": True},
    })
    workflow["current_stage"] = stage
    workflow["stage_history"] = history
    run_payload["workflow"] = workflow


def append_run_event_local(cfg: Config, run_id: str, event_type: str, state: str, message: str, metadata: dict[str, Any] | None = None) -> None:
    append_jsonl(
        event_log_path(cfg, run_id),
        {
            "timestamp": now_iso(),
            "run_id": run_id,
            "event_type": event_type,
            "state": state,
            "message": message,
            "metadata": metadata or {},
        },
    )


def set_run_terminal_or_blocked(cfg: Config, run_id: str, run_payload: dict[str, Any], *, state: str, blocked_reason: str | None = None, result_preview: str | None = None) -> None:
    run_payload["state"] = state
    run_payload["updated_at"] = now_iso()
    run_payload["completed_at"] = run_payload.get("completed_at") or now_iso()
    run_payload["blocked_reason"] = blocked_reason if state == "blocked" else None
    run_payload["result_preview"] = result_preview if state == "completed" else run_payload.get("result_preview")
    atomic_write_json(run_path(cfg, run_id), run_payload)
    append_run_event_local(cfg, run_id, "delivery_reconciled", state, f"Delivery reconciled to {state}", {"blocked_reason": blocked_reason})


def reconcile_delivery(cfg: Config, run_id: str, run_payload: dict[str, Any]) -> dict[str, Any]:
    stage = str(((run_payload.get("workflow") or {}).get("current_stage") or "")).lower()
    blocked_reason = str(run_payload.get("blocked_reason") or "").lower()
    if stage not in {"opening_pr", "monitoring_ci", "ready_for_review"}:
        return {"status": "skipped", "reason": "workflow-stage-not-reconcilable"}
    if blocked_reason not in DELIVERY_BLOCKERS:
        return {"status": "skipped", "reason": "blocked-reason-not-reconcilable"}
    repo_info = run_payload.get("repo") or {}
    repo_path = Path(repo_info.get("path") or repo_info.get("key", "").replace("repo://", ""))
    remote = git_remote_for_repo(repo_path)
    if not remote:
        return {"status": "skipped", "reason": "no-github-remote"}
    owner, repo = remote
    delivery = dict((run_payload.get("metadata") or {}).get("delivery") or {})
    branch = (delivery.get("pr") or {}).get("branch") or repo_info.get("working_branch")
    if not branch:
        proc = subprocess.run(["git", "-C", str(repo_path), "branch", "--show-current"], capture_output=True, text=True, check=False, timeout=20)
        branch = proc.stdout.strip() if proc.returncode == 0 else None
    if not branch:
        return {"status": "skipped", "reason": "no-branch"}
    pr = find_pr(owner, repo, branch)
    if not pr:
        return {"status": "skipped", "reason": "no-pr-found", "branch": branch}
    ci = ci_status(owner, repo, pr["head_sha"])
    delivery["pr"] = {
        **dict(delivery.get("pr") or {}),
        **pr,
        "transport": "api-basic",
        "status": "opened",
    }
    delivery["ci"] = {
        **dict(delivery.get("ci") or {}),
        **ci,
        "transport": "api-basic",
        "sha": pr["head_sha"],
    }
    delivery["reconciled"] = {"at": now_iso(), "reason": "tasklane-manual-reconciliation"}
    metadata = dict(run_payload.get("metadata") or {})
    metadata["delivery"] = delivery
    run_payload["metadata"] = metadata
    if ci["status"] == "pass":
        update_workflow_stage(run_payload, "ready_for_review", "Tasklane reconciliation confirmed PR and green CI")
        set_run_terminal_or_blocked(cfg, run_id, run_payload, state="completed", result_preview=f"Reconciled PR {pr['url']} with green CI")
        return {"status": "completed", "pr": pr, "ci": ci}
    update_workflow_stage(run_payload, "monitoring_ci", "Tasklane reconciliation found PR with non-terminal CI")
    set_run_terminal_or_blocked(cfg, run_id, run_payload, state="blocked", blocked_reason="ci-pending" if ci["status"] == "pending" else f"ci-{ci['status']}")
    return {"status": "blocked", "pr": pr, "ci": ci}


def finalize_submitted_task(cfg: Config, task_uid: str, entry: dict[str, Any], destination_dir: Path, note: dict[str, Any]) -> None:
    raw_source_path = entry.get("source_path")
    if raw_source_path:
        source_path = Path(raw_source_path)
    else:
        source_path = None
    if source_path and source_path.exists():
        moved = move_task_file(source_path, destination_dir)
        note_path = moved.with_suffix(moved.suffix + ".result.json")
        atomic_write_json(note_path, note)


def result_text(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    parts: list[str] = []
    if isinstance(result, dict):
        for key in ("final_response", "summary", "output", "message"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    elif isinstance(result, str):
        parts.append(result)
    error = payload.get("last_error")
    if isinstance(error, str) and error.strip():
        parts.append(error)
    return "\n\n".join(parts)


def review_gate_decision(payload: dict[str, Any]) -> str | None:
    text = result_text(payload)
    for line in text.splitlines():
        match = re.search(r"TASKLANE_REVIEW_DECISION:\s*(pass|needs-fix)", line, flags=re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return None


def review_blocker_classification(payload: dict[str, Any]) -> dict[str, str]:
    text = result_text(payload)
    lowered = text.lower()
    if any(pattern in lowered for pattern in REVIEW_HUMAN_DECISION_PATTERNS):
        return {"class": "needs-human", "reason": "product-secret-deploy-decision"}
    if any(pattern in lowered for pattern in REVIEW_EVIDENCE_ONLY_PATTERNS):
        return {"class": "evidence-only", "reason": "verification-or-pr-evidence"}
    return {"class": "concrete-fix", "reason": "review-finding-is-actionable"}


def review_gate_spec_from_root_job(root_job: dict[str, Any], iteration: int, dependency_job_id: str) -> dict[str, Any]:
    root_spec = root_job.get("spec") or {}
    branch = root_spec.get("branch") or {}
    repo = root_spec.get("repo") or {}
    request = root_spec.get("request") or {}
    metadata = root_spec.get("metadata") or {}
    root_uid = str(metadata.get("uid") or root_job.get("id") or "")
    root_job_id = str(root_job.get("id") or "")
    job_id = review_job_id(root_uid, iteration)
    prompt = "\n".join(
        [
            "Codex PR review gate.",
            "",
            "Review the implementation independently. Do not edit files, commit, push, merge, deploy, or open another PR.",
            f"Implementation job: {root_job_id}",
            f"Review iteration: {iteration}",
            f"Base branch: {branch.get('base_branch')}",
            f"Work branch: {branch.get('work_branch')}",
            "",
            "Mandatory review context:",
            format_review_docs(parse_csv(str(metadata.get("review_docs") or ""))),
            "",
            "Required verification commands:",
            format_required_verification(parse_csv(str(metadata.get("required_verification_commands") or ""))),
            "",
            "Compare the implementation against the issue, admin/project instructions, architecture docs, product vision docs, and existing conventions.",
            "Inspect unresolved GitHub PR review threads and comments. Treat unresolved P0/P1/P2 findings as needs-fix unless the latest diff clearly makes the comment obsolete; if obsolete, say why.",
            "Verify that the PR body evidence matches the current head SHA, current diffstat, exact required commands, and current test counts.",
            "Use needs-fix if the PR is technically green but violates documented product or architecture intent.",
            "Use needs-fix if required verification was not run, failed, or does not cover the touched areas. Quote the failing/missing command and expected fix.",
            "",
            "Return one exact decision line near the top:",
            "TASKLANE_REVIEW_DECISION: pass",
            "or",
            "TASKLANE_REVIEW_DECISION: needs-fix",
            "",
            "Use needs-fix for correctness bugs, missing acceptance criteria, missing required tests, broken build/typecheck/lint, unsafe behavior, or scope creep.",
            "After the decision line, include findings with file/line references where possible, verification evidence, and residual risks.",
            "",
            "Original task:",
            str(request.get("body") or ""),
        ]
    )
    return {
        "schema_version": 1,
        "id": job_id,
        "source": {"type": "tasklane-review-gate", "label": root_spec.get("project"), "task_file": (root_spec.get("source") or {}).get("task_file")},
        "project": root_spec.get("project"),
        "repo": repo,
        "request": {"type": "task-small", "title": f"Review {request.get('title') or root_job_id}", "body": prompt},
        "branch": {"mode": "existing-branch", "base_branch": branch.get("base_branch"), "work_branch": branch.get("work_branch"), "pr_target": None},
        "delivery_mode": "report-only",
        "dependencies": [dependency_job_id],
        "pipeline": {"role": "codex-review-gate", **dict(root_spec.get("pipeline") or {})},
        "scope": dict(root_spec.get("scope") or {}),
        "metadata": {
            **metadata,
            "source": "tasklane-review-gate",
            "uid": f"{root_uid}:codex-review:{iteration}",
            "root_uid": root_uid,
            "root_job_id": root_job_id,
            "review_iteration": iteration,
        },
    }


def merge_gate_enabled_for_root(root_job: dict[str, Any]) -> bool:
    metadata = ((root_job.get("spec") or {}).get("metadata") or {})
    return bool_from_any(metadata.get("merge_gate"), default=False) or bool_from_any(metadata.get("auto_merge"), default=False)


def merge_gate_spec_from_root_job(root_job: dict[str, Any], dependency_job_id: str) -> dict[str, Any]:
    root_spec = root_job.get("spec") or {}
    branch = root_spec.get("branch") or {}
    repo = root_spec.get("repo") or {}
    request = root_spec.get("request") or {}
    metadata = root_spec.get("metadata") or {}
    auto_merge = bool_from_any(metadata.get("auto_merge"), default=False)
    root_uid = str(metadata.get("uid") or root_job.get("id") or "")
    root_job_id = str(root_job.get("id") or "")
    job_id = merge_job_id(root_uid)
    prompt = "\n".join(
        [
            "Tasklane PR merge gate.",
            "",
            "You are allowed to merge only the Tasklane-managed PR for this branch if every gate below passes.",
            f"Full auto-merge mode: {'enabled' if auto_merge else 'disabled'}.",
            "When full auto-merge mode is enabled, human approval is already granted by project policy; do not stop only to ask for approval.",
            f"Final PR job: {root_job_id}",
            f"Review gate dependency job: {dependency_job_id}",
            f"Base branch: {branch.get('base_branch')}",
            f"Work branch: {branch.get('work_branch')}",
            "",
            "Mandatory context:",
            format_review_docs(parse_csv(str(metadata.get("review_docs") or ""))),
            "",
            "Required verification commands:",
            format_required_verification(parse_csv(str(metadata.get("required_verification_commands") or ""))),
            "",
            "Before merging, verify:",
            "- PR branch starts with tasklane/ and targets the configured base branch.",
            "- CI/checks are green or every required verification command above is green where CI is unavailable.",
            "- Tasklane Codex review gate passed. The review gate dependency job above is authoritative Tasklane evidence; do not reject only because there is no GitHub-visible review comment.",
            "- The PR body matches the current head SHA, diffstat, changed files, exact required commands, and latest test counts.",
            "- No unresolved P0/P1/P2 GitHub PR review thread remains unless the latest diff clearly makes it obsolete and that is documented.",
            "- No unresolved P0/P1 review finding, security concern, data-loss risk, missing migration, secret/config blocker, or product/design ambiguity remains.",
            "- Implementation matches the issue, admin.md, docs, architecture, and PR body evidence.",
            "",
            "If the Tasklane-managed PR is still draft but all other gates pass and the final PR evidence is complete, mark it ready for review before merging.",
            "If every gate passes, merge the PR using the repository's normal merge method, close GitHub issues clearly referenced by the PR as completed, delete the remote branch if safe, and report TASKLANE_MERGE_DECISION: merged.",
            "If any gate is not satisfied, do not merge. Report TASKLANE_MERGE_DECISION: needs-human and include the exact blocker and required user action.",
            "Never use needs-human for a blank approval request in full auto-merge mode; only use it for a concrete blocker such as failing checks, conflicts, product ambiguity, secrets, or unsafe deploy/contract work.",
            "",
            "Original final PR task:",
            str(request.get("body") or ""),
        ]
    )
    return {
        "schema_version": 1,
        "id": job_id,
        "source": {"type": "tasklane-merge-gate", "label": root_spec.get("project"), "task_file": (root_spec.get("source") or {}).get("task_file")},
        "project": root_spec.get("project"),
        "repo": repo,
        "request": {"type": "task-small", "title": f"Merge gate for {request.get('title') or root_job_id}", "body": prompt},
        "branch": {"mode": "existing-branch", "base_branch": branch.get("base_branch"), "work_branch": branch.get("work_branch"), "pr_target": None},
        "delivery_mode": "report-only",
        "dependencies": [dependency_job_id],
        "pipeline": {"role": "tasklane-merge-gate", **dict(root_spec.get("pipeline") or {})},
        "scope": dict(root_spec.get("scope") or {}),
        "metadata": {
            **metadata,
            "source": "tasklane-merge-gate",
            "uid": f"{root_uid}:merge-gate",
            "root_uid": root_uid,
            "root_job_id": root_job_id,
        },
    }


def merge_gate_decision(payload: dict[str, Any]) -> str | None:
    text = result_text(payload)
    for line in text.splitlines():
        match = re.search(r"TASKLANE_MERGE_DECISION:\s*(merged|needs-human)", line, flags=re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return None


def github_pr_reference_from_job(job: dict[str, Any]) -> dict[str, Any] | None:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    delivery = result.get("delivery_validation") if isinstance(result.get("delivery_validation"), dict) else {}
    pr = delivery.get("pr") if isinstance(delivery.get("pr"), dict) else {}
    text = "\n".join(str(value or "") for value in (pr.get("url"), result.get("final_response"), result.get("summary"), result.get("output")))
    match = re.search(r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", text)
    if not match:
        return None
    owner, repo, number = match.group(1), match.group(2), int(match.group(3))
    return {"owner": owner, "repo": repo, "number": number, "url": f"https://github.com/{owner}/{repo}/pull/{number}"}


def github_pr_merge_status_from_job(job: dict[str, Any]) -> dict[str, Any]:
    ref = github_pr_reference_from_job(job)
    if not ref:
        return {"status": "unknown", "reason": "pr-reference-missing"}
    try:
        pr = github_pull_request(str(ref["owner"]), str(ref["repo"]), int(ref["number"]))
    except Exception as exc:
        return {"status": "unknown", "reason": "github-pr-lookup-failed", "error": str(exc), "pr": ref}
    merged_at = pr.get("merged_at")
    state = str(pr.get("state") or "").lower()
    payload = {
        "status": "merged" if merged_at else "closed" if state == "closed" else "open" if state == "open" else "unknown",
        "state": state or None,
        "merged_at": merged_at,
        "url": pr.get("html_url") or ref.get("url"),
        "number": pr.get("number") or ref.get("number"),
        "draft": pr.get("draft"),
    }
    return payload


def review_fix_spec_from_jobs(root_job: dict[str, Any], review_job: dict[str, Any], iteration: int) -> dict[str, Any]:
    root_spec = root_job.get("spec") or {}
    branch = root_spec.get("branch") or {}
    repo = root_spec.get("repo") or {}
    request = root_spec.get("request") or {}
    metadata = root_spec.get("metadata") or {}
    root_uid = str(metadata.get("uid") or root_job.get("id") or "")
    root_job_id = str(root_job.get("id") or "")
    review_job_id_value = str(review_job.get("id") or "")
    job_id = fix_job_id(root_uid, iteration)
    prompt = "\n".join(
        [
            "Fix the Codex review gate findings on the existing PR branch.",
            "",
            f"Implementation job: {root_job_id}",
            f"Review job: {review_job_id_value}",
            f"Fix iteration: {iteration}",
            f"Base branch: {branch.get('base_branch')}",
            f"Work branch: {branch.get('work_branch')}",
            "",
            "Only fix issues raised by the review. Do not expand scope. Preserve existing project conventions.",
            "Run the strongest practical verification after changes and leave the PR review-ready.",
            "",
            "Review findings:",
            result_text(review_job),
            "",
            "Original task:",
            str(request.get("body") or ""),
        ]
    )
    return {
        "schema_version": 1,
        "id": job_id,
        "source": {"type": "tasklane-review-fix", "label": root_spec.get("project"), "task_file": (root_spec.get("source") or {}).get("task_file")},
        "project": root_spec.get("project"),
        "repo": repo,
        "request": {"type": "task-small", "title": f"Fix review findings for {request.get('title') or root_job_id}", "body": prompt},
        "branch": {"mode": "existing-branch", "base_branch": branch.get("base_branch"), "work_branch": branch.get("work_branch"), "pr_target": None},
        "delivery_mode": "direct-push",
        "dependencies": [review_job_id_value],
        "pipeline": {"role": "codex-review-fix", **dict(root_spec.get("pipeline") or {})},
        "scope": dict(root_spec.get("scope") or {}),
        "metadata": {
            **metadata,
            "source": "tasklane-review-fix",
            "uid": f"{root_uid}:codex-fix:{iteration}",
            "root_uid": root_uid,
            "root_job_id": root_job_id,
            "review_iteration": iteration,
            "parent_review_job_id": review_job_id_value,
        },
    }


def queue_synthetic_job(cfg: Config, job_id: str, spec: dict[str, Any], *, reason: str) -> Path:
    ready_path = job_path(cfg, job_id, "ready")
    if not find_job_record(cfg, job_id):
        atomic_write_json(ready_path, synthetic_job_record(job_id, spec))
        append_jsonl(job_event_log_path(cfg, job_id), {"timestamp": now_iso(), "job_id": job_id, "event_type": "job_created", "state": "ready", "reason": reason})
    return ready_path


def command_reconcile(cfg: Config) -> int:
    ensure_layout(cfg)
    state = load_state(cfg)
    submitted = dict(state.get("submitted") or {})
    remaining: dict[str, Any] = {}
    actions: list[dict[str, Any]] = []
    for task_uid, entry in submitted.items():
        job_id = entry.get("job_id") or entry.get("run_id")
        run_id = entry.get("run_id") or job_id
        if not job_id:
            actions.append({"task_uid": task_uid, "status": "missing-job-id"})
            continue
        job_payload = find_job_record(cfg, job_id)
        if isinstance(job_payload, dict):
            job_state = str(job_payload.get("state") or "").lower()
            if job_state == "completed":
                if entry.get("kind") == "codex-review":
                    root_uid = str(entry.get("root_uid") or "")
                    root_entry = dict(remaining.get(root_uid) or submitted.get(root_uid) or {})
                    root_job = find_job_record(cfg, str(root_entry.get("job_id") or ""))
                    decision = review_gate_decision(job_payload)
                    iteration = int(entry.get("review_iteration") or 1)
                    gate = dict(root_entry.get("review_gate") or {})
                    if not root_uid or not isinstance(root_job, dict):
                        actions.append({"task_uid": task_uid, "status": "review-gate-needs-human", "job_id": job_id, "reason": "root-job-missing"})
                        continue
                    if decision == "pass":
                        guard_findings = github_pr_gate_findings(root_job, review_text=result_text(job_payload))
                        if guard_findings:
                            guarded_review_job = review_gate_guard_payload(job_payload, guard_findings)
                            max_loops = int(gate.get("max_loops") or ((root_job.get("spec") or {}).get("pipeline") or {}).get("budgets", {}).get("review_loops") or 1)
                            if iteration >= max_loops:
                                root_spec = root_job.get("spec") or {}
                                extra_limit = max(0, project_int_setting(cfg, root_spec.get("project"), "extra_review_fix_loops", default=0))
                                extra_count = int(gate.get("extra_fix_count") or 0)
                                if extra_count >= extra_limit:
                                    gate.update(
                                        {
                                            "enabled": True,
                                            "status": "needs-human",
                                            "decision_job_id": job_id,
                                            "reason": "tasklane-guard-findings",
                                            "current_iteration": iteration,
                                            "guard_findings": guard_findings,
                                            "updated_at": now_iso(),
                                        }
                                    )
                                    root_entry["review_gate"] = gate
                                    remaining[root_uid] = root_entry
                                    finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "review_decision": decision, "guard_findings": guard_findings})
                                    actions.append({"task_uid": task_uid, "status": "review-gate-needs-human", "job_id": job_id, "root_uid": root_uid, "reason": "tasklane-guard-findings", "guard_findings": guard_findings})
                                    continue
                                fix_id = fix_job_id(root_uid, iteration)
                                fix_path = queue_synthetic_job(cfg, fix_id, review_fix_spec_from_jobs(root_job, guarded_review_job, iteration), reason="tasklane-review-gate-guard-extra-fix")
                                next_iteration = iteration + 1
                                next_review_id = review_job_id(root_uid, next_iteration)
                                next_review_path = queue_synthetic_job(cfg, next_review_id, review_gate_spec_from_root_job(root_job, next_iteration, fix_id), reason="tasklane-review-gate-guard-extra-review")
                                gate.update(
                                    {
                                        "enabled": True,
                                        "status": "fixing",
                                        "decision_job_id": job_id,
                                        "fix_job_id": fix_id,
                                        "review_job_id": next_review_id,
                                        "current_iteration": next_iteration,
                                        "extra_fix_count": extra_count + 1,
                                        "reason": "tasklane-guard-findings",
                                        "guard_findings": guard_findings,
                                        "updated_at": now_iso(),
                                    }
                                )
                                root_entry["review_gate"] = gate
                                remaining[root_uid] = root_entry
                                remaining[f"{root_uid}:codex-fix:{iteration}"] = {
                                    "synthetic": True,
                                    "kind": "codex-review-fix",
                                    "root_uid": root_uid,
                                    "job_id": fix_id,
                                    "run_id": fix_id,
                                    "repo_key": root_entry.get("repo_key"),
                                    "submitted_at": now_iso(),
                                    "review_iteration": iteration,
                                }
                                remaining[f"{root_uid}:codex-review:{next_iteration}"] = {
                                    "synthetic": True,
                                    "kind": "codex-review",
                                    "root_uid": root_uid,
                                    "job_id": next_review_id,
                                    "run_id": next_review_id,
                                    "repo_key": root_entry.get("repo_key"),
                                    "submitted_at": now_iso(),
                                    "review_iteration": next_iteration,
                                }
                                finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "review_decision": decision, "guard_findings": guard_findings})
                                actions.append({"task_uid": task_uid, "status": "review-gate-guard-fix-queued", "job_id": job_id, "root_uid": root_uid, "fix_job_id": fix_id, "fix_job_file": str(fix_path), "next_review_job_id": next_review_id, "next_review_job_file": str(next_review_path), "guard_findings": guard_findings})
                                continue
                            fix_id = fix_job_id(root_uid, iteration)
                            fix_path = queue_synthetic_job(cfg, fix_id, review_fix_spec_from_jobs(root_job, guarded_review_job, iteration), reason="tasklane-review-gate-guard-fix")
                            next_iteration = iteration + 1
                            next_review_id = review_job_id(root_uid, next_iteration)
                            next_review_path = queue_synthetic_job(cfg, next_review_id, review_gate_spec_from_root_job(root_job, next_iteration, fix_id), reason="tasklane-review-gate-guard-review")
                            gate.update(
                                {
                                    "enabled": True,
                                    "status": "fixing",
                                    "decision_job_id": job_id,
                                    "fix_job_id": fix_id,
                                    "review_job_id": next_review_id,
                                    "current_iteration": next_iteration,
                                    "reason": "tasklane-guard-findings",
                                    "guard_findings": guard_findings,
                                    "updated_at": now_iso(),
                                }
                            )
                            root_entry["review_gate"] = gate
                            remaining[root_uid] = root_entry
                            remaining[f"{root_uid}:codex-fix:{iteration}"] = {
                                "synthetic": True,
                                "kind": "codex-review-fix",
                                "root_uid": root_uid,
                                "job_id": fix_id,
                                "run_id": fix_id,
                                "repo_key": root_entry.get("repo_key"),
                                "submitted_at": now_iso(),
                                "review_iteration": iteration,
                            }
                            remaining[f"{root_uid}:codex-review:{next_iteration}"] = {
                                "synthetic": True,
                                "kind": "codex-review",
                                "root_uid": root_uid,
                                "job_id": next_review_id,
                                "run_id": next_review_id,
                                "repo_key": root_entry.get("repo_key"),
                                "submitted_at": now_iso(),
                                "review_iteration": next_iteration,
                            }
                            finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "review_decision": decision, "guard_findings": guard_findings})
                            actions.append({"task_uid": task_uid, "status": "review-gate-guard-fix-queued", "job_id": job_id, "root_uid": root_uid, "fix_job_id": fix_id, "fix_job_file": str(fix_path), "next_review_job_id": next_review_id, "next_review_job_file": str(next_review_path), "guard_findings": guard_findings})
                            continue
                        gate.update({"enabled": True, "status": "passed", "decision_job_id": job_id, "passed_at": now_iso(), "current_iteration": iteration})
                        root_entry["review_gate"] = gate
                        if merge_gate_enabled_for_root(root_job):
                            merge_id = merge_job_id(root_uid)
                            merge_path = queue_synthetic_job(cfg, merge_id, merge_gate_spec_from_root_job(root_job, job_id), reason="tasklane-merge-gate")
                            root_entry["merge_gate"] = {
                                "enabled": True,
                                "status": "queued",
                                "merge_job_id": merge_id,
                                "queued_at": now_iso(),
                            }
                            remaining[f"{root_uid}:merge-gate"] = {
                                "synthetic": True,
                                "kind": "tasklane-merge-gate",
                                "root_uid": root_uid,
                                "job_id": merge_id,
                                "run_id": merge_id,
                                "repo_key": root_entry.get("repo_key"),
                                "submitted_at": now_iso(),
                            }
                            actions.append({"task_uid": task_uid, "status": "merge-gate-queued", "job_id": job_id, "root_uid": root_uid, "merge_job_id": merge_id, "merge_job_file": str(merge_path)})
                        else:
                            actions.append({"task_uid": task_uid, "status": "review-gate-passed", "job_id": job_id, "root_uid": root_uid})
                        remaining[root_uid] = root_entry
                        finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "review_decision": decision})
                        continue
                    if decision == "needs-fix":
                        max_loops = int(gate.get("max_loops") or ((root_job.get("spec") or {}).get("pipeline") or {}).get("budgets", {}).get("review_loops") or 1)
                        if iteration >= max_loops:
                            root_spec = root_job.get("spec") or {}
                            extra_limit = max(0, project_int_setting(cfg, root_spec.get("project"), "extra_review_fix_loops", default=0))
                            extra_count = int(gate.get("extra_fix_count") or 0)
                            blocker_classification = review_blocker_classification(job_payload)
                            if blocker_classification.get("class") == "needs-human":
                                extra_limit = 0
                            if extra_count < extra_limit:
                                fix_id = fix_job_id(root_uid, iteration)
                                fix_path = queue_synthetic_job(cfg, fix_id, review_fix_spec_from_jobs(root_job, job_payload, iteration), reason="tasklane-review-gate-extra-fix")
                                next_iteration = iteration + 1
                                next_review_id = review_job_id(root_uid, next_iteration)
                                next_review_path = queue_synthetic_job(cfg, next_review_id, review_gate_spec_from_root_job(root_job, next_iteration, fix_id), reason="tasklane-review-gate-extra-review")
                                gate.update(
                                    {
                                        "enabled": True,
                                        "status": "fixing",
                                        "decision_job_id": job_id,
                                        "fix_job_id": fix_id,
                                        "review_job_id": next_review_id,
                                        "current_iteration": next_iteration,
                                        "extra_fix_count": extra_count + 1,
                                        "reason": "extra-review-fix-after-loop-cap",
                                        "updated_at": now_iso(),
                                    }
                                )
                                root_entry["review_gate"] = gate
                                remaining[root_uid] = root_entry
                                remaining[f"{root_uid}:codex-fix:{iteration}"] = {
                                    "synthetic": True,
                                    "kind": "codex-review-fix",
                                    "root_uid": root_uid,
                                    "job_id": fix_id,
                                    "run_id": fix_id,
                                    "repo_key": root_entry.get("repo_key"),
                                    "submitted_at": now_iso(),
                                    "review_iteration": iteration,
                                }
                                remaining[f"{root_uid}:codex-review:{next_iteration}"] = {
                                    "synthetic": True,
                                    "kind": "codex-review",
                                    "root_uid": root_uid,
                                    "job_id": next_review_id,
                                    "run_id": next_review_id,
                                    "repo_key": root_entry.get("repo_key"),
                                    "submitted_at": now_iso(),
                                    "review_iteration": next_iteration,
                                }
                                finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "review_decision": decision})
                                actions.append(
                                    {
                                        "task_uid": task_uid,
                                        "status": "review-gate-extra-fix-queued",
                                        "job_id": job_id,
                                        "root_uid": root_uid,
                                        "fix_job_id": fix_id,
                                        "fix_job_file": str(fix_path),
                                        "next_review_job_id": next_review_id,
                                        "next_review_job_file": str(next_review_path),
                                        "extra_fix_count": extra_count + 1,
                                        "extra_fix_limit": extra_limit,
                                    }
                                )
                                continue
                            blocker_classification = locals().get("blocker_classification") or review_blocker_classification(job_payload)
                            gate.update({"enabled": True, "status": "needs-human", "decision_job_id": job_id, "reason": "max-review-loops-reached", "current_iteration": iteration, "blocker_classification": blocker_classification})
                            root_entry["review_gate"] = gate
                            remaining[root_uid] = root_entry
                            finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "review_decision": decision})
                            actions.append({"task_uid": task_uid, "status": "review-gate-needs-human", "job_id": job_id, "root_uid": root_uid, "reason": "max-review-loops-reached", "blocker_classification": blocker_classification})
                            continue
                        fix_id = fix_job_id(root_uid, iteration)
                        fix_path = queue_synthetic_job(cfg, fix_id, review_fix_spec_from_jobs(root_job, job_payload, iteration), reason="tasklane-review-gate-fix")
                        next_iteration = iteration + 1
                        next_review_id = review_job_id(root_uid, next_iteration)
                        next_review_path = queue_synthetic_job(cfg, next_review_id, review_gate_spec_from_root_job(root_job, next_iteration, fix_id), reason="tasklane-review-gate-repeat")
                        gate.update(
                            {
                                "enabled": True,
                                "status": "fixing",
                                "decision_job_id": job_id,
                                "fix_job_id": fix_id,
                                "review_job_id": next_review_id,
                                "current_iteration": next_iteration,
                                "updated_at": now_iso(),
                            }
                        )
                        root_entry["review_gate"] = gate
                        remaining[root_uid] = root_entry
                        remaining[f"{root_uid}:codex-fix:{iteration}"] = {
                            "synthetic": True,
                            "kind": "codex-review-fix",
                            "root_uid": root_uid,
                            "job_id": fix_id,
                            "run_id": fix_id,
                            "repo_key": root_entry.get("repo_key"),
                            "submitted_at": now_iso(),
                            "review_iteration": iteration,
                        }
                        remaining[f"{root_uid}:codex-review:{next_iteration}"] = {
                            "synthetic": True,
                            "kind": "codex-review",
                            "root_uid": root_uid,
                            "job_id": next_review_id,
                            "run_id": next_review_id,
                            "repo_key": root_entry.get("repo_key"),
                            "submitted_at": now_iso(),
                            "review_iteration": next_iteration,
                        }
                        finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "review_decision": decision})
                        actions.append(
                            {
                                "task_uid": task_uid,
                                "status": "review-gate-fix-queued",
                                "job_id": job_id,
                                "root_uid": root_uid,
                                "fix_job_id": fix_id,
                                "fix_job_file": str(fix_path),
                                "next_review_job_id": next_review_id,
                                "next_review_job_file": str(next_review_path),
                            }
                        )
                        continue
                    gate.update({"enabled": True, "status": "needs-human", "decision_job_id": job_id, "reason": "review-decision-missing", "current_iteration": iteration})
                    root_entry["review_gate"] = gate
                    remaining[root_uid] = root_entry
                    finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "review_decision": None})
                    actions.append({"task_uid": task_uid, "status": "review-gate-needs-human", "job_id": job_id, "root_uid": root_uid, "reason": "review-decision-missing"})
                    continue
                if entry.get("kind") == "codex-review-fix":
                    finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state})
                    actions.append({"task_uid": task_uid, "status": "review-fix-completed", "job_id": job_id, "root_uid": entry.get("root_uid")})
                    continue
                if entry.get("kind") == "tasklane-merge-gate":
                    root_uid = str(entry.get("root_uid") or "")
                    root_entry = dict(remaining.get(root_uid) or submitted.get(root_uid) or {})
                    decision = merge_gate_decision(job_payload)
                    merge_gate = dict(root_entry.get("merge_gate") or {})
                    if decision == "merged":
                        merge_gate.update({"enabled": True, "status": "merged", "decision_job_id": job_id, "merged_at": now_iso()})
                        root_entry["merge_gate"] = merge_gate
                        remaining[root_uid] = root_entry
                        finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "merge_decision": decision})
                        actions.append({"task_uid": task_uid, "status": "merge-gate-merged", "job_id": job_id, "root_uid": root_uid})
                        continue
                    merge_gate.update({"enabled": True, "status": "needs-human", "decision_job_id": job_id, "reason": decision or "merge-decision-missing", "updated_at": now_iso()})
                    root_entry["merge_gate"] = merge_gate
                    remaining[root_uid] = root_entry
                    finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "merge_decision": decision})
                    actions.append({"task_uid": task_uid, "status": "merge-gate-needs-human", "job_id": job_id, "root_uid": root_uid, "reason": decision or "merge-decision-missing"})
                    continue
                gate = dict(entry.get("review_gate") or {})
                if gate.get("enabled"):
                    if gate.get("status") == "passed":
                        merge_gate = dict(entry.get("merge_gate") or {})
                        if merge_gate.get("enabled") and merge_gate.get("status") != "merged":
                            manual_merge = github_pr_merge_status_from_job(job_payload)
                            if manual_merge.get("status") == "merged":
                                merge_gate.update(
                                    {
                                        "enabled": True,
                                        "status": "merged",
                                        "reason": "manual-merge-detected",
                                        "merged_at": manual_merge.get("merged_at") or now_iso(),
                                        "updated_at": now_iso(),
                                        "pr": manual_merge,
                                    }
                                )
                                result = dict(job_payload.get("result") or {})
                                result["review_gate"] = gate
                                result["merge_gate"] = merge_gate
                                finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "result": result})
                                actions.append({"task_uid": task_uid, "status": "completed-manual-merge-detected", "job_id": job_id, "merge_gate": merge_gate})
                                continue
                            remaining[task_uid] = entry
                            actions.append({"task_uid": task_uid, "status": "waiting-merge-gate", "job_id": job_id, "merge_gate": merge_gate})
                            continue
                        result = dict(job_payload.get("result") or {})
                        result["review_gate"] = gate
                        if merge_gate:
                            result["merge_gate"] = merge_gate
                        finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "result": result})
                        actions.append({"task_uid": task_uid, "status": "completed-review-gate-passed", "job_id": job_id, "review_job_id": gate.get("decision_job_id"), "merge_job_id": (merge_gate or {}).get("merge_job_id")})
                        continue
                    remaining[task_uid] = entry
                    actions.append({"task_uid": task_uid, "status": "waiting-review-gate", "job_id": job_id, "review_gate": gate})
                    continue
                result = dict(job_payload.get("result") or {})
                finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "result": result})
                actions.append({"task_uid": task_uid, "status": "completed", "job_id": job_id})
                continue
            if job_state == "failed":
                classification = classify_failed_job(cfg, job_payload, int(cfg.watch.get("max_retry_attempts") or 3))
                if classification.get("classification") == "salvage-needed":
                    salvage = auto_salvage_failed_job(cfg, job_payload, auto=bool(cfg.watch.get("auto_salvage", False)))
                    actions.append({"task_uid": task_uid, "status": "auto-salvage", **salvage})
                    if salvage.get("status") == "salvaged":
                        continue
                    remaining[task_uid] = entry
                    continue
                finalize_submitted_task(cfg, task_uid, entry, cfg.failed_dir, {"job_id": job_id, "state": job_state, "error": job_payload.get("last_error")})
                actions.append({"task_uid": task_uid, "status": "failed", "job_id": job_id, "error": job_payload.get("last_error")})
                continue
            if job_state in {"ready", "running", "blocked", "needs-human"}:
                remaining[task_uid] = entry
                actions.append({"task_uid": task_uid, "status": "still-active", "job_id": job_id, "job_state": job_state, "error": job_payload.get("last_error")})
                continue
        # Backward compatibility for tasklane submissions created before the
        # Hermes v10 JobStore migration.
        run_payload = load_json(run_path(cfg, run_id))
        if not isinstance(run_payload, dict):
            actions.append({"task_uid": task_uid, "status": "missing-job-record"})
            remaining[task_uid] = entry
            continue
        run_state = str(run_payload.get("state") or "").lower()
        if run_state == "blocked":
            result = reconcile_delivery(cfg, run_id, run_payload)
            actions.append({"task_uid": task_uid, "status": "delivery-reconciled", **result})
            run_payload = load_json(run_path(cfg, run_id)) or run_payload
            run_state = str(run_payload.get("state") or "").lower()
        if run_state == "completed":
            finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"run_id": run_id, "state": run_state, "result_preview": run_payload.get("result_preview")})
            actions.append({"task_uid": task_uid, "status": "completed", "run_id": run_id})
            continue
        if run_state == "blocked":
            current_stage = str(((run_payload.get("workflow") or {}).get("current_stage") or "")).lower()
            current_blocked_reason = str(run_payload.get("blocked_reason") or "").lower()
            if current_blocked_reason in DELIVERY_BLOCKERS and current_stage in {"opening_pr", "monitoring_ci", "ready_for_review"}:
                remaining[task_uid] = entry
                actions.append(
                    {
                        "task_uid": task_uid,
                        "status": "waiting-delivery",
                        "run_id": run_id,
                        "blocked_reason": run_payload.get("blocked_reason"),
                        "stage": (run_payload.get("workflow") or {}).get("current_stage"),
                    }
                )
                continue
            finalize_submitted_task(cfg, task_uid, entry, cfg.failed_dir, {"run_id": run_id, "state": run_state, "blocked_reason": run_payload.get("blocked_reason"), "error": run_payload.get("error")})
            actions.append({"task_uid": task_uid, "status": "failed", "run_id": run_id, "blocked_reason": run_payload.get("blocked_reason")})
            continue
        if run_state == "cancelled":
            finalize_submitted_task(cfg, task_uid, entry, cfg.cancelled_dir, {"run_id": run_id, "state": run_state, "cancelled_reason": run_payload.get("cancelled_reason")})
            actions.append({"task_uid": task_uid, "status": "cancelled", "run_id": run_id})
            continue
        remaining[task_uid] = entry
        actions.append({"task_uid": task_uid, "status": "still-active", "run_id": run_id, "run_state": run_state})
    state["submitted"] = remaining
    save_state(cfg, state)
    print(json.dumps({"actions": actions}, indent=2))
    return 0


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def minutes_since(value: Any) -> int | None:
    parsed = parse_timestamp(value)
    if not parsed:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds() // 60))


def auto_salvage_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") or {}
    if isinstance(result, dict):
        auto_salvage = result.get("auto_salvage")
        if isinstance(auto_salvage, dict):
            return auto_salvage
    return {}


def failed_command_summaries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for item in results:
        if item.get("ok"):
            continue
        failures.append(
            {
                "command": item.get("command"),
                "exit_code": item.get("exit_code"),
                "phase": item.get("phase"),
                "output_tail": item.get("output_tail"),
                "baseline": {
                    "ok": (item.get("baseline") or {}).get("ok"),
                    "exit_code": (item.get("baseline") or {}).get("exit_code"),
                    "matches_branch_output": (item.get("baseline") or {}).get("matches_branch_output"),
                }
                if isinstance(item.get("baseline"), dict)
                else None,
            }
        )
    return failures


def verification_summary_for_job(payload: dict[str, Any]) -> dict[str, Any] | None:
    auto_salvage = auto_salvage_result(payload)
    if not auto_salvage:
        return None
    verification = [item for item in auto_salvage.get("verification") or [] if isinstance(item, dict)]
    bootstrap = [item for item in auto_salvage.get("bootstrap") or [] if isinstance(item, dict)]
    failed_verification = failed_command_summaries(verification)
    failed_bootstrap = failed_command_summaries(bootstrap)
    accepted = [
        {
            "command": item.get("command"),
            "status": item.get("status"),
        }
        for item in verification
        if item.get("accepted_baseline_failure")
    ]
    return {
        "status": auto_salvage.get("status") or ("failed" if failed_bootstrap or failed_verification else "passed"),
        "reason": auto_salvage.get("reason"),
        "failed_bootstrap": failed_bootstrap,
        "failed_verification": failed_verification,
        "accepted_baseline_failures": accepted,
        "changed_files": auto_salvage.get("changed_files") or ((auto_salvage.get("inspection") or {}).get("changed_files") if isinstance(auto_salvage.get("inspection"), dict) else []),
    }


def job_liveness_summary(
    job: dict[str, Any],
    *,
    completed_ids: set[str] | None = None,
    now: datetime | None = None,
    process_alive: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    """Return derived, read-only liveness fields for a JobStore job record."""
    probe: Callable[[int], bool] = process_alive or process_is_alive
    completed = completed_ids or set()
    state = str(job.get("state") or "unknown")
    claimed_by = job.get("claimed_by")
    pid = claimant_pid(job)
    alive: bool | None = None
    if state == "running" and pid is not None:
        try:
            alive = bool(probe(pid))
        except Exception:
            alive = None
    waiting_for = waiting_dependencies(job, completed) if state == "ready" else []
    derived = "unknown"
    recovery_eligible = False
    recovery_blocked_reason = None
    if state == "running":
        if pid is None:
            derived = "running-unknown-claimant"
            recovery_blocked_reason = "claimant-pid-unknown"
        elif alive is True:
            derived = "running-alive"
            recovery_blocked_reason = "claimant-alive"
        elif alive is False:
            derived = "dead-claimant"
            recovery_eligible = True
        else:
            derived = "running-unknown-claimant"
            recovery_blocked_reason = "claimant-liveness-unknown"
    elif state == "ready":
        derived = "waiting-on-dependency" if waiting_for else "ready"
        if waiting_for:
            recovery_blocked_reason = "waiting-on-dependency"
    elif state == "blocked":
        derived = "blocked"
        recovery_blocked_reason = job.get("blocked_reason") or job.get("last_error")
    elif state == "needs-human":
        derived = "needs-human"
        recovery_blocked_reason = job.get("needs_human_reason") or job.get("last_error")
    elif state == "failed":
        derived = "failed"
    elif state == "completed":
        derived = "completed"
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    watchdog = metadata.get("watchdog_retry") if isinstance(metadata, dict) else None
    last_error = job.get("last_error")
    historical_last_error = None
    if watchdog and state in {"ready", "running"}:
        historical_last_error = last_error or (watchdog.get("last_error") if isinstance(watchdog, dict) else None)
        active_last_error = None
    else:
        active_last_error = last_error
    runtime_seconds = None
    if state == "running":
        claimed_at = parse_timestamp(job.get("claimed_at"))
        if claimed_at:
            current = now or datetime.now(timezone.utc)
            runtime_seconds = max(0, int((current - claimed_at).total_seconds()))
    return {
        "job_id": job.get("id"),
        "state": state,
        "derived_state": derived,
        "claimed_by": claimed_by,
        "claimant_pid": pid,
        "claimant_alive": alive,
        "runtime_seconds": runtime_seconds,
        "waiting_for": waiting_for,
        "recovery_eligible": recovery_eligible,
        "recovery_blocked_reason": recovery_blocked_reason,
        "last_error": active_last_error,
        "historical_last_error": historical_last_error,
        "last_watchdog_action": watchdog if isinstance(watchdog, dict) else None,
    }


def compact_pr_status_from_job(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    delivery = metadata.get("delivery") if isinstance(metadata, dict) else {}
    pr = (delivery or {}).get("pr") if isinstance(delivery, dict) else None
    if not isinstance(pr, dict):
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        pr = result.get("pr") if isinstance(result, dict) else None
    if isinstance(pr, dict) and (pr.get("url") or pr.get("number")):
        return {"status": "found", "url": pr.get("url"), "number": pr.get("number"), "head": pr.get("branch") or pr.get("head"), "base": pr.get("base_branch") or pr.get("base"), "message": "PR recorded in job metadata"}
    return {"status": "unknown", "url": None, "number": None, "head": None, "base": None, "message": "No cached PR visibility available"}


def operator_job_summary(
    payload: dict[str, Any],
    *,
    state: str | None = None,
    waiting_for: list[str] | None = None,
    completed_ids: set[str] | None = None,
    include_liveness: bool = True,
) -> dict[str, Any]:
    spec = payload.get("spec") or {}
    branch = spec.get("branch") or {}
    request = spec.get("request") or {}
    metadata = payload.get("metadata") or {}
    salvage = metadata.get("tasklane_salvage") if isinstance(metadata, dict) else {}
    delivery = metadata.get("delivery") if isinstance(metadata, dict) else {}
    raw_pr = (delivery or {}).get("pr") if isinstance(delivery, dict) else None
    summary = {
        "id": payload.get("id"),
        "state": state or payload.get("state"),
        "attempt": payload.get("attempt"),
        "project": spec.get("project"),
        "repo_key": ((spec.get("repo") or {}).get("key")),
        "title": request.get("title"),
        "mode": branch.get("mode"),
        "base_branch": branch.get("base_branch"),
        "work_branch": branch.get("work_branch"),
        "delivery_mode": spec.get("delivery_mode"),
        "waiting_for": waiting_for or [],
        "claimed_by": payload.get("claimed_by"),
        "runtime_minutes": minutes_since(payload.get("claimed_at")) if payload.get("state") == "running" else None,
        "error": payload.get("last_error"),
        "needs_human_reason": payload.get("needs_human_reason"),
        "salvage": salvage if isinstance(salvage, dict) else None,
        "verification": verification_summary_for_job(payload),
        "pr": raw_pr,
        "pr_status": compact_pr_status_from_job(payload),
    }
    if include_liveness:
        live = job_liveness_summary(payload, completed_ids=completed_ids)
        if waiting_for is not None:
            live["waiting_for"] = waiting_for
            if waiting_for and live.get("derived_state") == "ready":
                live["derived_state"] = "waiting-on-dependency"
        summary.update({k: live.get(k) for k in ["derived_state", "claimant_pid", "claimant_alive", "runtime_seconds", "last_watchdog_action", "historical_last_error", "recovery_eligible", "recovery_blocked_reason"]})
        summary["waiting_for"] = live.get("waiting_for") or waiting_for or []
        if live.get("last_error") is None and live.get("historical_last_error") is not None:
            summary["error"] = None
    return summary


def compact_job(
    payload: dict[str, Any],
    *,
    state: str | None = None,
    waiting_for: list[str] | None = None,
    completed_ids: set[str] | None = None,
) -> dict[str, Any]:
    return operator_job_summary(payload, state=state, waiting_for=waiting_for, completed_ids=completed_ids)


def systemd_gateway_status() -> dict[str, Any]:
    if not shutil.which("systemctl"):
        return {"available": False, "state": "unknown", "ok": None, "reason": "systemctl-not-found"}
    proc = subprocess.run(
        ["systemctl", "is-active", "hermes-gateway.service"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    state = proc.stdout.strip() or proc.stderr.strip() or "unknown"
    return {"available": True, "state": state, "ok": proc.returncode == 0 and state == "active"}


def claimant_pid(payload: dict[str, Any]) -> int | None:
    claimed_by = str(payload.get("claimed_by") or "")
    match = re.fullmatch(r"gateway-(\d+)", claimed_by)
    return int(match.group(1)) if match else None


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def watch_expected_base_map(cfg: Config, overrides: list[str] | None = None) -> dict[str, str]:
    configured = dict(cfg.watch.get("expected_base_branches") or {})
    for item in overrides or []:
        if "=" not in item:
            raise ValueError(f"invalid --expected-base value {item!r}; expected PROJECT_OR_REPO=BRANCH")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise ValueError(f"invalid --expected-base value {item!r}; expected PROJECT_OR_REPO=BRANCH")
        configured[key] = value
    return configured


def expected_base_for_job(job: dict[str, Any], expected: dict[str, str]) -> str | None:
    spec = job.get("spec") or {}
    repo = spec.get("repo") or {}
    candidates = [
        str(repo.get("key") or ""),
        str(repo.get("path") or ""),
        str(spec.get("project") or ""),
    ]
    for candidate in candidates:
        if candidate in expected:
            return expected[candidate]
    return None


def watch_ignored_blocked_jobs(cfg: Config, overrides: list[str] | None = None) -> set[str]:
    ignored = {str(item) for item in cfg.watch.get("ignored_blocked_jobs") or []}
    ignored.update(str(item) for item in overrides or [])
    return ignored


def add_watch_problem(
    problems: list[dict[str, Any]],
    severity: str,
    code: str,
    message: str,
    job: dict[str, Any] | None = None,
    *,
    completed_ids: set[str] | None = None,
) -> None:
    entry: dict[str, Any] = {"severity": severity, "code": code, "message": message}
    if job:
        entry["job"] = compact_job(job, completed_ids=completed_ids)
    problems.append(entry)


def job_repo_path_from_spec(job: dict[str, Any]) -> Path | None:
    spec = job.get("spec") or {}
    repo = spec.get("repo") or {}
    raw = str(repo.get("path") or repo.get("key") or "").strip()
    if raw.startswith("repo://"):
        raw = raw.removeprefix("repo://")
    if not raw:
        return None
    return Path(raw).expanduser()


def job_worktree_path(cfg: Config, job: dict[str, Any]) -> Path | None:
    job_id = str(job.get("id") or "").strip()
    metadata = job.get("metadata") or {}
    for candidate in (
        ((metadata.get("workspace") or {}).get("worktree_path") if isinstance(metadata.get("workspace"), dict) else None),
        metadata.get("worktree_path"),
    ):
        if candidate:
            return Path(str(candidate)).expanduser()
    for event in reversed(read_job_events(cfg, job_id)):
        event_meta = event.get("metadata") or {}
        if isinstance(event_meta, dict) and event_meta.get("worktree_path"):
            return Path(str(event_meta["worktree_path"])).expanduser()
    return job_repo_path_from_spec(job)


def git_stdout(repo_path: Path, args: list[str], *, timeout: int = 30) -> tuple[bool, str]:
    proc = run_process(["git", "-C", str(repo_path), *args], timeout=timeout)
    output = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode == 0, output


def git_ref_exists(repo_path: Path, ref: str) -> bool:
    proc = run_process(["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", ref], timeout=20)
    return proc.returncode == 0


def git_base_ref(repo_path: Path, base_branch: str | None) -> str | None:
    if not base_branch:
        return None
    candidates = [f"origin/{base_branch}", base_branch]
    for candidate in candidates:
        if git_ref_exists(repo_path, candidate):
            return candidate
    ok, upstream = git_stdout(repo_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    return upstream if ok and upstream else None


def parse_porcelain_changed_files(output: str) -> list[str]:
    changed: list[str] = []
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        if len(raw_line) > 3 and raw_line[2] == " ":
            path = raw_line[3:]
        else:
            parts = raw_line.split(maxsplit=1)
            path = parts[1] if len(parts) == 2 else raw_line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip()
        if path:
            changed.append(path)
    return sorted(set(changed))


def path_matches(path: str, patterns: list[str]) -> bool:
    clean = path.strip().lstrip("./")
    for pattern in patterns:
        prefix = pattern.strip().lstrip("./").rstrip("/")
        if not prefix:
            continue
        if clean == prefix or clean.startswith(prefix + "/"):
            return True
    return False


def scope_violations(job: dict[str, Any], changed_files: list[str]) -> list[dict[str, Any]]:
    spec = job.get("spec") or {}
    scope = spec.get("scope") or {}
    allowed = [str(item) for item in scope.get("allowed_paths") or []]
    denied = [str(item) for item in scope.get("denied_paths") or []]
    allow_unlisted = bool(scope.get("allow_unlisted_paths", True))
    findings: list[dict[str, Any]] = []
    for path in changed_files:
        if path_matches(path, denied):
            findings.append({"code": "denied-path-changed", "path": path})
        if not allow_unlisted and allowed and not path_matches(path, allowed):
            findings.append({"code": "unlisted-path-changed", "path": path})
        if not allow_unlisted and not allowed:
            findings.append({"code": "restricted-scope-without-allowed-paths", "path": path})
    return findings


def inspect_job_worktree(cfg: Config, job: dict[str, Any]) -> dict[str, Any]:
    spec = job.get("spec") or {}
    branch = spec.get("branch") or {}
    expected_work_branch = str(branch.get("work_branch") or "").strip()
    expected_base_branch = str(branch.get("base_branch") or branch.get("pr_target") or "").strip()
    worktree = job_worktree_path(cfg, job)
    if not worktree:
        return {"ok": False, "reason": "worktree-path-missing", "has_changes": False}
    if not worktree.exists():
        return {"ok": False, "reason": "worktree-missing", "worktree_path": str(worktree), "has_changes": False}
    if not (worktree / ".git").exists():
        ok, root = git_stdout(worktree, ["rev-parse", "--show-toplevel"])
        if not ok:
            return {"ok": False, "reason": "not-a-git-worktree", "worktree_path": str(worktree), "has_changes": False}
        worktree = Path(root)
    ok, current_branch = git_stdout(worktree, ["branch", "--show-current"])
    if not ok:
        current_branch = ""
    ok, status = git_stdout(worktree, ["status", "--porcelain"])
    if not ok:
        return {"ok": False, "reason": "git-status-failed", "worktree_path": str(worktree), "has_changes": False}
    dirty_files = parse_porcelain_changed_files(status)
    base_ref = git_base_ref(worktree, expected_base_branch)
    committed_files: list[str] = []
    ahead_count = 0
    if base_ref:
        ok, ahead = git_stdout(worktree, ["rev-list", "--count", f"{base_ref}..HEAD"])
        if ok and ahead.isdigit():
            ahead_count = int(ahead)
        ok, diff_files = git_stdout(worktree, ["diff", "--name-only", f"{base_ref}...HEAD"])
        if ok and diff_files:
            committed_files = sorted({line.strip() for line in diff_files.splitlines() if line.strip()})
    changed_files = sorted(set(dirty_files + committed_files))
    violations = scope_violations(job, changed_files)
    branch_ok = not expected_work_branch or current_branch == expected_work_branch
    return {
        "ok": True,
        "worktree_path": str(worktree),
        "current_branch": current_branch,
        "expected_work_branch": expected_work_branch,
        "base_branch": expected_base_branch,
        "base_ref": base_ref,
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files,
        "ahead_count": ahead_count,
        "committed_files": committed_files,
        "changed_files": changed_files,
        "has_changes": bool(dirty_files or ahead_count > 0),
        "branch_ok": branch_ok,
        "scope_violations": violations,
    }


def salvageable_error(job: dict[str, Any]) -> bool:
    error = str(job.get("last_error") or "").lower()
    return any(pattern in error for pattern in SALVAGE_ERROR_PATTERNS)


def classify_failed_job(cfg: Config, job: dict[str, Any], max_attempts: int) -> dict[str, Any]:
    if str(job.get("state") or "").lower() != "failed":
        return {"classification": "not-failed"}
    inspection = inspect_job_worktree(cfg, job)
    if inspection.get("has_changes"):
        if salvageable_error(job):
            return {"classification": "salvage-needed", "inspection": inspection}
        return {"classification": "needs-human", "reason": "dirty-worktree-unclassified-error", "inspection": inspection}
    ok, reason = safe_to_retry(job, max_attempts)
    if ok:
        return {"classification": "retryable-clean", "reason": reason, "inspection": inspection}
    return {"classification": "terminal-failed", "reason": reason, "inspection": inspection}


def verification_commands_for_job(cfg: Config, job: dict[str, Any]) -> list[str]:
    commands = configured_commands_for_job(cfg, job, "verification")
    if "git diff --check" not in commands:
        commands.insert(0, "git diff --check")
    return commands


def bootstrap_commands_for_job(cfg: Config, job: dict[str, Any]) -> list[str]:
    return configured_commands_for_job(cfg, job, "bootstrap")


def configured_commands_for_job(cfg: Config, job: dict[str, Any], kind: str) -> list[str]:
    spec = job.get("spec") or {}
    metadata = spec.get("metadata") or {}
    configured = cfg.watch.get(f"{kind}_commands")
    project = str(spec.get("project") or "").strip()
    repo_path = job_repo_path_from_spec(job)
    repo_name = repo_path.name if repo_path else ""
    profiles = cfg.watch.get(f"{kind}_profiles") or {}

    allow_task_command_overrides = bool_from_any(cfg.watch.get("allow_task_command_overrides"), default=False)
    raw: Any = metadata.get(f"{kind}_commands") if allow_task_command_overrides else None
    raw = raw or configured
    profile_name = metadata.get(f"{kind}_profile") or cfg.watch.get(f"{kind}_profile")
    for key in (project, repo_name, profile_name):
        if key and isinstance(profiles, dict) and key in profiles:
            raw = profiles[key]
            break

    commands: list[str] = []
    if isinstance(raw, str):
        commands.extend(parse_csv(raw))
    elif isinstance(raw, list):
        commands.extend(str(item).strip() for item in raw if str(item).strip())
    return commands


def command_output_tail(output: str, *, limit: int = 3000) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    text = "\n".join(lines[-40:])
    return text[-limit:] if len(text) > limit else text


def command_output_compare_text(output: str, roots: list[Path]) -> str:
    text = command_output_tail(output, limit=6000)
    for root in roots:
        if root:
            text = text.replace(str(root), "<repo>")
    text = re.sub(r"pytest-\d+", "pytest-N", text)
    text = re.sub(r"/tmp/[^\s'\"]+", "<tmp>", text)
    return text.strip()


def run_verification_commands(worktree: Path, commands: list[str], *, timeout: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for command in commands:
        started = now_iso()
        try:
            proc = run_shell_command(command, cwd=worktree, timeout=timeout)
            output = "\n".join(part for part in [proc.stdout, proc.stderr] if part)
            results.append(
                {
                    "command": command,
                    "started_at": started,
                    "completed_at": now_iso(),
                    "exit_code": proc.returncode,
                    "ok": proc.returncode == 0,
                    "output_tail": command_output_tail(output),
                }
            )
        except subprocess.TimeoutExpired as exc:
            output = "\n".join(part.decode() if isinstance(part, bytes) else str(part or "") for part in [exc.stdout, exc.stderr] if part)
            results.append(
                {
                    "command": command,
                    "started_at": started,
                    "completed_at": now_iso(),
                    "exit_code": None,
                    "ok": False,
                    "timed_out": True,
                    "output_tail": command_output_tail(output),
                }
            )
            break
    return results


def run_labeled_commands(worktree: Path, commands: list[str], *, timeout: int, phase: str) -> list[dict[str, Any]]:
    results = run_verification_commands(worktree, commands, timeout=timeout)
    for result in results:
        result["phase"] = phase
    return results


def baseline_verification_enabled(cfg: Config, job: dict[str, Any]) -> bool:
    metadata = (job.get("spec") or {}).get("metadata") or {}
    if "baseline_verification" in metadata:
        return bool_from_any(metadata.get("baseline_verification"), default=False)
    return bool_from_any(cfg.watch.get("baseline_verification"), default=False)


def allow_matching_baseline_failures(cfg: Config, job: dict[str, Any]) -> bool:
    metadata = (job.get("spec") or {}).get("metadata") or {}
    if "allow_matching_baseline_failures" in metadata:
        return bool_from_any(metadata.get("allow_matching_baseline_failures"), default=False)
    return bool_from_any(cfg.watch.get("allow_matching_baseline_failures"), default=False)


def baseline_worktree_path(cfg: Config, job_id: str) -> Path:
    safe_job_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", job_id)
    return cfg.task_root / "baseline-worktrees" / safe_job_id


def prepare_baseline_worktree(source_worktree: Path, target_path: Path, base_ref: str) -> dict[str, Any]:
    if target_path.exists():
        cleanup_baseline_worktree(source_worktree, target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    proc = run_process(["git", "-C", str(source_worktree), "worktree", "add", "--detach", "--force", str(target_path), base_ref], timeout=180)
    output = command_output_tail((proc.stdout or "") + (proc.stderr or ""))
    return {"ok": proc.returncode == 0, "path": str(target_path), "base_ref": base_ref, "exit_code": proc.returncode, "output_tail": output}


def cleanup_baseline_worktree(source_worktree: Path, target_path: Path) -> None:
    if target_path.exists():
        run_process(["git", "-C", str(source_worktree), "worktree", "remove", "--force", str(target_path)], timeout=120)
    if target_path.exists():
        shutil.rmtree(target_path, ignore_errors=True)


def annotate_verification_with_baseline(
    cfg: Config,
    job: dict[str, Any],
    *,
    worktree: Path,
    base_ref: str | None,
    bootstrap_commands: list[str],
    verification: list[dict[str, Any]],
    timeout: int,
) -> list[dict[str, Any]]:
    if not baseline_verification_enabled(cfg, job) or not base_ref:
        return verification
    failed = [result for result in verification if not result.get("ok")]
    if not failed:
        return verification

    baseline_path = baseline_worktree_path(cfg, str(job.get("id") or "job"))
    prepared = prepare_baseline_worktree(worktree, baseline_path, base_ref)
    if not prepared.get("ok"):
        for result in failed:
            result["baseline"] = prepared
        return verification
    try:
        baseline_bootstrap = run_labeled_commands(
            baseline_path,
            bootstrap_commands,
            timeout=int(cfg.watch.get("bootstrap_timeout_seconds") or timeout),
            phase="baseline-bootstrap",
        )
        bootstrap_ok = all(result.get("ok") for result in baseline_bootstrap)
        for result in failed:
            baseline_result = {
                "command": result.get("command"),
                "phase": "baseline-verification",
                "skipped": not bootstrap_ok,
                "bootstrap": baseline_bootstrap,
            }
            if bootstrap_ok:
                baseline_run = run_verification_commands(baseline_path, [str(result.get("command") or "")], timeout=timeout)
                baseline_result = baseline_run[0] if baseline_run else baseline_result
                baseline_result["phase"] = "baseline-verification"
                branch_text = command_output_compare_text(str(result.get("output_tail") or ""), [worktree, baseline_path])
                baseline_text = command_output_compare_text(str(baseline_result.get("output_tail") or ""), [worktree, baseline_path])
                matches = bool(branch_text and baseline_text and branch_text == baseline_text)
                baseline_result["matches_branch_output"] = matches
                if not baseline_result.get("ok") and matches and allow_matching_baseline_failures(cfg, job):
                    result["ok"] = True
                    result["accepted_baseline_failure"] = True
                    result["status"] = "accepted-baseline-failure"
            result["baseline"] = baseline_result
    finally:
        cleanup_baseline_worktree(worktree, baseline_path)
    return verification


def run_delivery_checks(cfg: Config, job: dict[str, Any], worktree: Path, inspection: dict[str, Any]) -> dict[str, Any]:
    timeout = int(cfg.watch.get("verification_timeout_seconds") or 1800)
    bootstrap_timeout = int(cfg.watch.get("bootstrap_timeout_seconds") or timeout)
    bootstrap_commands = bootstrap_commands_for_job(cfg, job)
    verification_commands = verification_commands_for_job(cfg, job)
    bootstrap = run_labeled_commands(worktree, bootstrap_commands, timeout=bootstrap_timeout, phase="bootstrap")
    if bootstrap and not all(result.get("ok") for result in bootstrap):
        return {
            "ok": False,
            "reason": "bootstrap-failed",
            "bootstrap": bootstrap,
            "verification": [],
        }
    verification = run_labeled_commands(worktree, verification_commands, timeout=timeout, phase="verification")
    verification = annotate_verification_with_baseline(
        cfg,
        job,
        worktree=worktree,
        base_ref=inspection.get("base_ref"),
        bootstrap_commands=bootstrap_commands,
        verification=verification,
        timeout=timeout,
    )
    return {
        "ok": bool(verification) and all(result.get("ok") for result in verification),
        "reason": None if verification and all(result.get("ok") for result in verification) else "verification-failed",
        "bootstrap": bootstrap,
        "verification": verification,
    }


def git_commit_if_needed(worktree: Path, message: str) -> dict[str, Any]:
    ok, status = git_stdout(worktree, ["status", "--porcelain"])
    if not ok:
        return {"ok": False, "reason": "git-status-failed"}
    if not status:
        ok, sha = git_stdout(worktree, ["rev-parse", "HEAD"])
        return {"ok": True, "committed": False, "sha": sha}
    add = run_process(["git", "-C", str(worktree), "add", "-A"], timeout=120)
    if add.returncode != 0:
        return {"ok": False, "reason": "git-add-failed", "output": command_output_tail((add.stdout or "") + (add.stderr or ""))}
    commit = run_process(["git", "-C", str(worktree), "commit", "-m", message], timeout=120)
    if commit.returncode != 0:
        return {"ok": False, "reason": "git-commit-failed", "output": command_output_tail((commit.stdout or "") + (commit.stderr or ""))}
    ok, sha = git_stdout(worktree, ["rev-parse", "HEAD"])
    return {"ok": ok, "committed": True, "sha": sha, "output": command_output_tail((commit.stdout or "") + (commit.stderr or ""))}


def push_branch(worktree: Path, branch: str) -> dict[str, Any]:
    proc = run_process(["git", "-C", str(worktree), "push", "-u", "origin", f"HEAD:{branch}"], timeout=180)
    output = command_output_tail((proc.stdout or "") + (proc.stderr or ""))
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": output}


def salvage_pr_body(job: dict[str, Any], verification: list[dict[str, Any]], inspection: dict[str, Any]) -> str:
    spec = job.get("spec") or {}
    request = spec.get("request") or {}
    command_lines = "\n".join(f"- `{result['command']}`: {'passed' if result.get('ok') else 'failed'}" for result in verification)
    changed = "\n".join(f"- `{path}`" for path in inspection.get("changed_files") or [])
    return "\n".join(
        [
            "## Summary",
            "",
            str(request.get("body") or request.get("title") or "Tasklane salvaged delivery."),
            "",
            "## Tasklane Auto-Salvage",
            "",
            f"- Job: `{job.get('id')}`",
            f"- Original failure: {job.get('last_error') or 'unknown'}",
            "- Reason: provider/job failure happened after code changes were produced.",
            "",
            "## Changed Files",
            "",
            changed or "- No changed files reported.",
            "",
            "## Verification",
            "",
            command_lines or "- No verification commands configured.",
        ]
    )


def complete_tasklane_record_for_job(cfg: Config, job_id: str, note: dict[str, Any]) -> dict[str, Any] | None:
    state = load_state(cfg)
    submitted = dict(state.get("submitted") or {})
    for task_uid, entry in list(submitted.items()):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("job_id") or entry.get("run_id") or "") != job_id:
            continue
        finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, note)
        submitted.pop(task_uid, None)
        state["submitted"] = submitted
        save_state(cfg, state)
        return {"task_uid": task_uid, "source": "submitted"}

    for result_path in sorted(cfg.failed_dir.glob("*.result.json")):
        result = load_json(result_path)
        if not isinstance(result, dict) or str(result.get("job_id") or result.get("run_id") or "") != job_id:
            continue
        task_name = result_path.name[: -len(".result.json")]
        task_path = result_path.with_name(task_name)
        moved_task: Path | None = None
        if task_path.exists():
            moved_task = move_task_file(task_path, cfg.completed_dir)
        result_path.unlink(missing_ok=True)
        note_path = (moved_task or (cfg.completed_dir / task_name)).with_suffix(Path(task_name).suffix + ".result.json")
        atomic_write_json(note_path, note)
        return {"task_uid": task_name, "source": "failed"}
    return None


def mark_job_completed_after_salvage(cfg: Config, job: dict[str, Any], result: dict[str, Any]) -> None:
    job_id = str(job.get("id") or "")
    source_path = find_job_record_path(cfg, job_id)
    payload = dict(job)
    payload["state"] = "completed"
    payload["updated_at"] = now_iso()
    payload["completed_at"] = payload.get("completed_at") or now_iso()
    payload["last_error"] = None
    payload["result"] = result
    metadata = dict(payload.get("metadata") or {})
    metadata["tasklane_salvage"] = {
        "status": "completed",
        "at": now_iso(),
        "reason": "dirty-worktree-auto-salvage",
    }
    payload["metadata"] = metadata
    atomic_write_json(job_path(cfg, job_id, "completed"), payload)
    if source_path and source_path != job_path(cfg, job_id, "completed"):
        source_path.unlink(missing_ok=True)
    append_jsonl(
        job_event_log_path(cfg, job_id),
        {
            "timestamp": now_iso(),
            "job_id": job_id,
            "event_type": "job_salvaged",
            "state": "completed",
            "reason": "tasklane-auto-salvage",
            "metadata": result,
        },
    )


def mark_job_needs_human_after_salvage(cfg: Config, job: dict[str, Any], result: dict[str, Any]) -> None:
    job_id = str(job.get("id") or "")
    source_path = find_job_record_path(cfg, job_id)
    payload = dict(job)
    payload["state"] = "needs-human"
    payload["updated_at"] = now_iso()
    payload["needs_human_at"] = payload.get("needs_human_at") or now_iso()
    payload["needs_human_reason"] = result.get("reason") or "auto-salvage-needs-human"
    payload["result"] = {"auto_salvage": result}
    metadata = dict(payload.get("metadata") or {})
    metadata["tasklane_salvage"] = {
        "status": "needs-human",
        "at": now_iso(),
        "reason": result.get("reason") or "auto-salvage-needs-human",
    }
    payload["metadata"] = metadata
    atomic_write_json(job_path(cfg, job_id, "needs-human"), payload)
    if source_path and source_path != job_path(cfg, job_id, "needs-human"):
        source_path.unlink(missing_ok=True)
    append_jsonl(
        job_event_log_path(cfg, job_id),
        {
            "timestamp": now_iso(),
            "job_id": job_id,
            "event_type": "job_needs_human",
            "state": "needs-human",
            "reason": "tasklane-auto-salvage",
            "metadata": result,
        },
    )


def auto_salvage_failed_job(cfg: Config, job: dict[str, Any], *, auto: bool) -> dict[str, Any]:
    job_id = str(job.get("id") or "")
    spec = job.get("spec") or {}
    branch = spec.get("branch") or {}
    delivery_mode = str(spec.get("delivery_mode") or "")

    def needs_human(reason: str, **extra: Any) -> dict[str, Any]:
        result = {"job_id": job_id, "status": "needs-human", "reason": reason, **extra}
        if auto:
            mark_job_needs_human_after_salvage(cfg, job, result)
        return result

    if delivery_mode not in SALVAGEABLE_DELIVERY_MODES:
        return {"job_id": job_id, "status": "skipped", "reason": "delivery-mode-not-salvageable"}
    if not salvageable_error(job):
        return {"job_id": job_id, "status": "skipped", "reason": "failure-not-salvageable"}
    inspection = inspect_job_worktree(cfg, job)
    if not inspection.get("ok"):
        return needs_human(str(inspection.get("reason") or "worktree-inspection-failed"), inspection=inspection)
    if not inspection.get("has_changes"):
        return {"job_id": job_id, "status": "skipped", "reason": "clean-worktree", "inspection": inspection}
    if not inspection.get("branch_ok"):
        return needs_human("work-branch-mismatch", inspection=inspection)
    if inspection.get("scope_violations"):
        return needs_human("scope-violations", inspection=inspection)
    if not auto:
        return {"job_id": job_id, "status": "salvage-needed", "inspection": inspection}

    worktree = Path(str(inspection["worktree_path"]))
    checks = run_delivery_checks(cfg, job, worktree, inspection)
    if not checks.get("ok"):
        return needs_human(
            str(checks.get("reason") or "verification-failed"),
            inspection=inspection,
            bootstrap=checks.get("bootstrap") or [],
            verification=checks.get("verification") or [],
        )

    inspection_after = inspect_job_worktree(cfg, job)
    if inspection_after.get("scope_violations"):
        return needs_human("scope-violations-after-verification", inspection=inspection_after, bootstrap=checks.get("bootstrap") or [], verification=checks.get("verification") or [])

    title = str((spec.get("request") or {}).get("title") or f"Tasklane salvage {job_id}")
    commit = git_commit_if_needed(worktree, f"{title}\n\nTasklane-auto-salvage: {job_id}")
    if not commit.get("ok"):
        return needs_human(str(commit.get("reason") or "git-commit-failed"), inspection=inspection_after, bootstrap=checks.get("bootstrap") or [], verification=checks.get("verification") or [], commit=commit)

    work_branch = str(branch.get("work_branch") or inspection_after.get("current_branch") or "").strip()
    base_branch = str(branch.get("pr_target") or branch.get("base_branch") or "").strip()
    if not work_branch or not base_branch:
        return needs_human("missing-branch-metadata", inspection=inspection_after, bootstrap=checks.get("bootstrap") or [], verification=checks.get("verification") or [], commit=commit)
    push = push_branch(worktree, work_branch)
    if not push.get("ok"):
        return needs_human("push-failed", inspection=inspection_after, bootstrap=checks.get("bootstrap") or [], verification=checks.get("verification") or [], commit=commit, push=push)

    remote = git_remote_for_repo(worktree)
    if not remote:
        return needs_human("no-github-remote", inspection=inspection_after, bootstrap=checks.get("bootstrap") or [], verification=checks.get("verification") or [], commit=commit, push=push)
    owner, repo = remote
    pr = find_pr(owner, repo, work_branch)
    if not pr:
        pr = create_pr(
            owner,
            repo,
            branch=work_branch,
            base_branch=base_branch,
            title=title,
            body=salvage_pr_body(job, checks.get("verification") or [], inspection_after),
        )
    if pr.get("base_branch") and pr.get("base_branch") != base_branch:
        return needs_human("pr-base-mismatch", pr=pr, inspection=inspection_after, bootstrap=checks.get("bootstrap") or [], verification=checks.get("verification") or [], commit=commit, push=push)

    final_inspection = inspect_job_worktree(cfg, job)
    result = {
        "delivery_validation": {
            "delivery_mode": delivery_mode,
            "ok": True,
            "pr": pr,
            "pr_target": base_branch,
            "work_branch": work_branch,
            "workspace": {
                "cleanup": "kept",
                "isolated": True,
                "original_repo_path": str(job_repo_path_from_spec(job) or ""),
                "worktree_path": str(worktree),
            },
            "worktree_clean": not final_inspection.get("dirty"),
        },
        "auto_salvage": {
            "reason": "provider/job failure happened after code changes were produced",
            "commit": commit.get("sha"),
            "bootstrap": checks.get("bootstrap") or [],
            "verification": checks.get("verification") or [],
            "changed_files": final_inspection.get("changed_files") or inspection_after.get("changed_files") or [],
        },
        "final_response": f"Tasklane auto-salvaged failed job {job_id} into PR {pr.get('url')}.",
    }
    mark_job_completed_after_salvage(cfg, job, result)
    completed_task = complete_tasklane_record_for_job(cfg, job_id, {"job_id": job_id, "state": "completed", "result": result})
    return {"job_id": job_id, "status": "salvaged", "pr": pr, "commit": commit, "push": push, "submitted_completed": completed_task}


def restore_submitted_task_for_retry(cfg: Config, job: dict[str, Any], ready_path: Path) -> dict[str, Any] | None:
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        return None
    state = load_state(cfg)
    submitted = dict(state.get("submitted") or {})
    if any(str(entry.get("job_id") or entry.get("run_id") or "") == job_id for entry in submitted.values() if isinstance(entry, dict)):
        return None

    result_path: Path | None = None
    task_path: Path | None = None
    for candidate in sorted(cfg.failed_dir.glob("*.result.json")):
        result = load_json(candidate)
        if not isinstance(result, dict) or str(result.get("job_id") or result.get("run_id") or "") != job_id:
            continue
        source_name = candidate.name[: -len(".result.json")]
        source_path = candidate.with_name(source_name)
        if source_path.exists():
            result_path = candidate
            task_path = source_path
            break
    if task_path is None:
        return None

    restored_path = move_task_file(task_path, cfg.submitted_dir)
    if result_path is not None:
        result_path.unlink(missing_ok=True)

    spec = dict(job.get("spec") or {})
    metadata = dict(spec.get("metadata") or {})
    task_uid = str(metadata.get("uid") or "").strip()
    if not task_uid:
        try:
            task_uid = load_task_file(restored_path, cfg).uid
        except Exception:
            task_uid = restored_path.stem
    submitted[task_uid] = {
        "source_path": str(restored_path),
        "original_name": restored_path.name,
        "job_id": job_id,
        "run_id": job_id,
        "job_file": str(ready_path),
        "repo_key": ((spec.get("repo") or {}).get("key") or ""),
        "submitted_at": now_iso(),
        "retried_at": now_iso(),
    }
    state["submitted"] = submitted
    save_state(cfg, state)
    return {"task_uid": task_uid, "source_path": str(restored_path)}


def retry_failed_job(cfg: Config, job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("id") or "")
    if not job_id:
        return {"status": "skipped", "reason": "missing-job-id"}
    failed_path = job_path(cfg, job_id, "failed")
    if not failed_path.exists():
        return {"job_id": job_id, "status": "skipped", "reason": "failed-record-missing"}
    payload = load_json(failed_path)
    if not isinstance(payload, dict):
        return {"job_id": job_id, "status": "skipped", "reason": "failed-record-invalid"}
    payload["state"] = "ready"
    payload["updated_at"] = now_iso()
    payload["last_error"] = None
    metadata = dict(payload.get("metadata") or {})
    metadata["watchdog_retry"] = {"at": now_iso(), "reason": "safe-transient-failure"}
    payload["metadata"] = metadata
    ready_path = job_path(cfg, job_id, "ready")
    atomic_write_json(ready_path, payload)
    failed_path.unlink()
    append_jsonl(
        job_event_log_path(cfg, job_id),
        {
            "timestamp": now_iso(),
            "job_id": job_id,
            "event_type": "job_state_changed",
            "state": "ready",
            "reason": "tasklane-watchdog-safe-retry",
        },
    )
    restored = restore_submitted_task_for_retry(cfg, payload, ready_path)
    action = {"job_id": job_id, "status": "retried", "from": str(failed_path), "to": str(ready_path)}
    if restored:
        action["submitted_restored"] = restored
    return action


def job_repo_path(job: dict[str, Any]) -> Path | None:
    spec = job.get("spec") or {}
    repo = spec.get("repo") or {}
    raw_path = repo.get("path")
    if not raw_path and isinstance(repo.get("key"), str) and str(repo.get("key")).startswith("repo://"):
        raw_path = str(repo.get("key"))[len("repo://") :]
    if not raw_path:
        return None
    return Path(str(raw_path)).expanduser()


def recover_blocked_stale_worktree_job(cfg: Config, job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("id") or "")
    if not job_id:
        return {"status": "skipped", "reason": "missing-job-id"}
    error = str(job.get("last_error") or "").lower()
    if not any(pattern in error for pattern in STALE_WORKTREE_ERROR_PATTERNS):
        return {"job_id": job_id, "status": "skipped", "reason": "not-stale-worktree-blocker"}
    repo_path = job_repo_path(job)
    if repo_path is None:
        return {"job_id": job_id, "status": "skipped", "reason": "repo-path-missing"}
    spec = job.get("spec") or {}
    branch = (spec.get("branch") or {}).get("work_branch")
    if not branch:
        return {"job_id": job_id, "status": "skipped", "reason": "work-branch-missing"}
    prune = git_worktree_prune(repo_path)
    if not prune.get("ok"):
        return {"job_id": job_id, "status": "skipped", "reason": "worktree-prune-failed", "prune": prune}
    branch_entries = git_worktree_branch_entries(repo_path, str(branch))
    if not branch_entries.get("ok"):
        return {"job_id": job_id, "status": "skipped", "reason": branch_entries.get("reason") or "worktree-list-failed", "branch_entries": branch_entries}
    if branch_entries.get("entries"):
        return {
            "job_id": job_id,
            "status": "skipped",
            "reason": "work-branch-still-checked-out",
            "branch_entries": branch_entries,
        }
    blocked_path = job_path(cfg, job_id, "blocked")
    if not blocked_path.exists():
        return {"job_id": job_id, "status": "skipped", "reason": "blocked-record-missing"}
    payload = load_json(blocked_path)
    if not isinstance(payload, dict):
        return {"job_id": job_id, "status": "skipped", "reason": "blocked-record-invalid"}
    payload["state"] = "ready"
    payload["updated_at"] = now_iso()
    payload["last_error"] = None
    payload.pop("claimed_at", None)
    payload.pop("claimed_by", None)
    payload.pop("failed_at", None)
    metadata = dict(payload.get("metadata") or {})
    metadata["watchdog_retry"] = {"at": now_iso(), "reason": "stale-worktree-pruned"}
    payload["metadata"] = metadata
    ready_path = job_path(cfg, job_id, "ready")
    atomic_write_json(ready_path, payload)
    blocked_path.unlink()
    append_jsonl(
        job_event_log_path(cfg, job_id),
        {
            "timestamp": now_iso(),
            "job_id": job_id,
            "event_type": "job_state_changed",
            "state": "ready",
            "reason": "tasklane-watchdog-stale-worktree-prune",
            "metadata": {"repo_path": str(repo_path), "work_branch": str(branch)},
        },
    )
    return {"job_id": job_id, "status": "retried", "reason": "stale-worktree-pruned", "from": str(blocked_path), "to": str(ready_path)}


def safe_to_retry(job: dict[str, Any], max_attempts: int) -> tuple[bool, str]:
    attempt = int(job.get("attempt") or 0)
    if attempt >= max_attempts:
        return False, "max-attempts-reached"
    error = str(job.get("last_error") or "").lower()
    if any(pattern in error for pattern in UNSAFE_RETRY_ERROR_PATTERNS):
        return False, "unsafe-error"
    if any(pattern in error for pattern in SAFE_RETRY_ERROR_PATTERNS):
        return True, "safe-transient-error"
    return False, "unclassified-error"


def compact_message_line(text: str, *, limit: int = 170) -> str:
    line = re.sub(r"\s+", " ", str(text or "").strip())
    if len(line) <= limit:
        return line
    return line[: max(0, limit - 3)].rstrip() + "..."


def first_actionable_finding(job: dict[str, Any] | None) -> str | None:
    if not isinstance(job, dict):
        return None
    for raw in result_text(job).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("TASKLANE_"):
            continue
        if re.match(r"^[-*]\s+\S+", line) or re.match(r"^\d+[.)]\s+\S+", line):
            return compact_message_line(line.lstrip("-* ").strip())
        if "blocker" in line.lower() or "needs" in line.lower() or "missing" in line.lower() or "failed" in line.lower():
            return compact_message_line(line)
    return None


def gate_operator_action(gate: str, reason: str, task_uid: str) -> str:
    if gate == "review":
        if reason == "max-review-loops-reached":
            return f"Do not merge yet. Ask Hermes to inspect `{task_uid}` and queue one focused fix, or close/defer the PR if the issue is not worth fixing now."
        return f"Do not merge yet. Ask Hermes to inspect `{task_uid}` and decide whether to queue a fix or defer it."
    if gate == "merge":
        return f"PR review passed but auto-merge stopped. If you approve the PR, merge it manually; then ask Hermes to reconcile Tasklane. If not, ask Hermes why `{task_uid}` was stopped."
    return f"Ask Hermes to inspect `{task_uid}` and propose the next step."


def submitted_gate_attention(cfg: Config) -> list[dict[str, Any]]:
    state = load_state(cfg)
    submitted = dict(state.get("submitted") or {})
    attention: list[dict[str, Any]] = []
    for task_uid, entry in submitted.items():
        if entry.get("synthetic"):
            continue
        for gate_key, label in (("review_gate", "review"), ("merge_gate", "merge")):
            gate = dict(entry.get(gate_key) or {})
            if gate.get("status") != "needs-human":
                continue
            root_job = find_job_record(cfg, str(entry.get("job_id") or ""))
            decision_job_id = gate.get("decision_job_id") or gate.get("review_job_id") or gate.get("merge_job_id") or entry.get("job_id")
            decision_job = find_job_record(cfg, str(decision_job_id or ""))
            pr_ref = github_pr_reference_from_job(root_job) if isinstance(root_job, dict) else None
            if isinstance(root_job, dict):
                merge_status = github_pr_merge_status_from_job(root_job)
                if merge_status.get("status") == "merged":
                    continue
            reason = str(gate.get("reason") or "needs-human")
            attention.append(
                {
                    "task_uid": task_uid,
                    "repo_key": entry.get("repo_key"),
                    "source_path": entry.get("source_path"),
                    "gate": label,
                    "status": gate.get("status"),
                    "reason": reason,
                    "job_id": decision_job_id,
                    "root_job_id": entry.get("job_id"),
                    "pr_url": (pr_ref or {}).get("url"),
                    "why": first_actionable_finding(decision_job),
                    "operator_action": gate_operator_action(label, reason, task_uid),
                    "current_iteration": gate.get("current_iteration"),
                    "max_loops": gate.get("max_loops"),
                    "extra_fix_count": gate.get("extra_fix_count"),
                }
            )
    return attention


def hermes_notification_target(cfg: Config) -> str:
    return str(cfg.watch.get("hermes_target") or cfg.watch.get("notify_target") or "telegram").strip()


def hermes_agent_python(cfg: Config) -> Path:
    configured = str(cfg.watch.get("hermes_agent_python") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return cfg.hermes_home / "hermes-agent" / "venv" / "bin" / "python"


def hermes_agent_path(cfg: Config) -> Path:
    configured = str(cfg.watch.get("hermes_agent_path") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return cfg.hermes_home / "hermes-agent"


def hermes_notification_config(cfg: Config) -> dict[str, Any]:
    target = hermes_notification_target(cfg)
    python_path = hermes_agent_python(cfg)
    agent_path = hermes_agent_path(cfg)
    env_path = cfg.hermes_home / ".env"
    return {
        "configured": bool(target) and python_path.exists() and (agent_path / "tools" / "send_message_tool.py").exists(),
        "target": target,
        "python": str(python_path),
        "agent_path": str(agent_path),
        "env_path": str(env_path),
        "env_file_exists": env_path.exists(),
    }


def telegram_notification_config(cfg: Config) -> dict[str, Any]:
    token_configured = bool(os.environ.get("TELEGRAM_BOT_TOKEN") or str(cfg.watch.get("telegram_bot_token") or "").strip())
    chat_configured = bool(str(cfg.watch.get("telegram_chat_id") or cfg.default_chat_id or "").strip())
    thread_configured = bool(str(cfg.watch.get("telegram_thread_id") or cfg.default_thread_id or "").strip())
    return {
        "configured": token_configured and chat_configured,
        "token_configured": token_configured,
        "chat_configured": chat_configured,
        "thread_configured": thread_configured,
    }


def notification_config(cfg: Config) -> dict[str, Any]:
    hermes_cfg = hermes_notification_config(cfg)
    telegram_cfg = telegram_notification_config(cfg)
    provider = str(cfg.watch.get("notification_provider") or "hermes").strip().lower()
    configured = hermes_cfg.get("configured") if provider == "hermes" else telegram_cfg.get("configured") if provider == "telegram" else hermes_cfg.get("configured") or telegram_cfg.get("configured")
    return {
        "configured": bool(configured),
        "provider": provider,
        "hermes": hermes_cfg,
        "telegram": telegram_cfg,
    }


def build_watch_report(
    cfg: Config,
    *,
    mode: str = "observe",
    stale_running_minutes: int | None = None,
    expected_base: dict[str, str] | None = None,
    ignored_blocked: set[str] | None = None,
    check_gateway: bool = True,
) -> dict[str, Any]:
    ensure_layout(cfg)
    stale_after = stale_running_minutes or int(cfg.watch.get("stale_running_minutes") or 180)
    expected = expected_base if expected_base is not None else watch_expected_base_map(cfg)
    ignored = ignored_blocked if ignored_blocked is not None else watch_ignored_blocked_jobs(cfg)
    jobs = iter_job_records(cfg)
    completed = completed_job_ids(cfg)
    by_state = {state: 0 for state in sorted(JOB_STATES)}
    for job in jobs:
        state = str(job.get("state") or "unknown")
        by_state[state] = by_state.get(state, 0) + 1
    counts_all = dict(by_state)
    by_state["waiting"] = 0
    counts_all["waiting"] = 0
    problems: list[dict[str, Any]] = []
    notices: list[dict[str, Any]] = []
    gateway = systemd_gateway_status() if check_gateway else {"available": False, "state": "unchecked", "ok": None}
    if check_gateway and gateway.get("ok") is False:
        add_watch_problem(problems, "critical", "gateway-inactive", f"hermes-gateway.service is {gateway.get('state')}")
    running = [job for job in jobs if job.get("state") == "running"]
    raw_ready = [job for job in jobs if job.get("state") == "ready"]
    ready, waiting, waiting_for = split_ready_jobs(cfg, raw_ready)
    counts_all["ready_physical"] = counts_all.get("ready", 0)
    by_state["ready"] = len(ready)
    by_state["waiting"] = len(waiting)
    counts_all["waiting"] = len(waiting)
    blocked = [job for job in jobs if job.get("state") == "blocked"]
    needs_human = [job for job in jobs if job.get("state") == "needs-human"]
    failed = [job for job in jobs if job.get("state") == "failed"]
    max_attempts = int(cfg.watch.get("max_retry_attempts") or 3)
    if ready and check_gateway and gateway.get("ok") is False:
        add_watch_problem(problems, "critical", "ready-jobs-without-gateway", f"{len(ready)} ready job(s) cannot run while the gateway is inactive")
    for job in running:
        runtime = minutes_since(job.get("claimed_at"))
        if runtime is None:
            add_watch_problem(problems, "warning", "running-missing-claimed-at", "running job has no claimed_at timestamp", job, completed_ids=completed)
        elif runtime > stale_after:
            add_watch_problem(problems, "warning", "running-stale", f"running job has exceeded {stale_after} minutes", job, completed_ids=completed)
        pid = claimant_pid(job)
        if pid is not None and not process_is_alive(pid):
            add_watch_problem(problems, "critical", "running-dead-claimant", f"claimed gateway process {pid} is not alive", job, completed_ids=completed)
    active_blocked: list[dict[str, Any]] = []
    ignored_blocked_jobs: list[dict[str, Any]] = []
    for job in blocked:
        job_id = str(job.get("id") or "")
        if job_id in ignored:
            ignored_blocked_jobs.append(job)
            notices.append({"code": "blocked-ignored", "job": compact_job(job, completed_ids=completed)})
            continue
        active_blocked.append(job)
        add_watch_problem(problems, "warning", "job-blocked", "job is blocked and needs review", job, completed_ids=completed)
    by_state["blocked"] = len(active_blocked)
    for job in needs_human:
        add_watch_problem(problems, "warning", "job-needs-human", "job is waiting for human input", job, completed_ids=completed)
    for job in failed:
        classification = classify_failed_job(cfg, job, max_attempts)
        kind = classification.get("classification")
        if kind == "salvage-needed":
            add_watch_problem(problems, "warning", "salvage-needed", "job failed after producing worktree changes; salvage instead of retry", job, completed_ids=completed)
            notices.append({"code": "salvage-needed", "job": compact_job(job, completed_ids=completed), "inspection": classification.get("inspection")})
        elif kind == "retryable-clean":
            add_watch_problem(problems, "warning", "job-retryable", "job failed with a clean transient error and can be retried", job, completed_ids=completed)
        elif kind == "needs-human":
            add_watch_problem(problems, "warning", "salvage-needs-human", str(classification.get("reason") or "dirty failed job needs review"), job, completed_ids=completed)
        else:
            add_watch_problem(problems, "warning", "job-failed", "job failed and was not automatically retried", job, completed_ids=completed)
    gate_attention = submitted_gate_attention(cfg)
    if gate_attention:
        by_state["needs-human"] = by_state.get("needs-human", 0) + len(gate_attention)
        counts_all["gate_attention"] = len(gate_attention)
        for gate in gate_attention:
            add_watch_problem(
                problems,
                "warning",
                f"{gate.get('gate')}-gate-needs-human",
                f"submitted Tasklane {gate.get('gate')} gate needs human action: {gate.get('reason')}",
                {"id": gate.get("job_id"), "state": "needs-human", "spec": {"project": gate.get("task_uid"), "request": {"title": gate.get("source_path")}}},
                completed_ids=completed,
            )
    notify_config = notification_config(cfg)
    if bool_from_any(cfg.watch.get("require_notifications"), default=False) and not notify_config.get("configured"):
        missing = []
        if not (notify_config.get("hermes") or {}).get("configured"):
            missing.append("Hermes send_message relay target")
        if not (notify_config.get("telegram") or {}).get("configured"):
            missing.append("direct Telegram token/chat fallback")
        add_watch_problem(problems, "critical", "notification-misconfigured", "Tasklane notifications are required but no delivery path is configured: " + ", ".join(missing))
    for job in ready + waiting + running:
        spec = job.get("spec") or {}
        branch = spec.get("branch") or {}
        expected_branch = expected_base_for_job(job, expected)
        if not expected_branch:
            continue
        base_branch = branch.get("base_branch")
        branch_mode = branch.get("mode")
        delivery_mode = spec.get("delivery_mode")
        if base_branch and base_branch != expected_branch:
            add_watch_problem(problems, "warning", "base-branch-mismatch", f"expected base branch {expected_branch!r}, got {base_branch!r}", job, completed_ids=completed)
        elif not base_branch and (branch_mode in {"new-branch", "detached-review"} or delivery_mode == "pull-request"):
            add_watch_problem(problems, "warning", "base-branch-missing", f"expected base branch {expected_branch!r}, but job has no base_branch", job, completed_ids=completed)
    health = "critical" if any(item["severity"] == "critical" for item in problems) else "warning" if problems else "ok"
    report = {
        "timestamp": now_iso(),
        "mode": mode,
        "health": health,
        "gateway": gateway,
        "counts": by_state,
        "counts_all": counts_all,
        "inbox": len([p for p in cfg.inbox_dir.iterdir() if p.is_file()]) if cfg.inbox_dir.exists() else 0,
        "running": [compact_job(job, completed_ids=completed) for job in running],
        "ready": [compact_job(job, completed_ids=completed) for job in ready],
        "waiting": [compact_job(job, state="waiting", waiting_for=waiting_for.get(str(job.get("id") or ""), []), completed_ids=completed) for job in waiting],
        "blocked": [compact_job(job, completed_ids=completed) for job in active_blocked],
        "ignored_blocked": [compact_job(job, completed_ids=completed) for job in ignored_blocked_jobs],
        "needs_human": [compact_job(job, completed_ids=completed) for job in needs_human],
        "gate_attention": gate_attention,
        "failed": [compact_job(job, completed_ids=completed) for job in failed],
        "notification_config": notify_config,
        "problems": problems,
        "notices": notices,
    }
    return report


def apply_guarded_watch_actions(cfg: Config, report: dict[str, Any]) -> list[dict[str, Any]]:
    max_attempts = int(cfg.watch.get("max_retry_attempts") or 3)
    auto_salvage = bool(cfg.watch.get("auto_salvage", False))
    actions: list[dict[str, Any]] = []
    for job in iter_job_records(cfg, {"blocked"}):
        action = recover_blocked_stale_worktree_job(cfg, job)
        if action.get("reason") == "not-stale-worktree-blocker":
            continue
        actions.append(action)
    for job in iter_job_records(cfg, {"failed"}):
        classification = classify_failed_job(cfg, job, max_attempts)
        kind = classification.get("classification")
        if kind == "salvage-needed":
            actions.append(auto_salvage_failed_job(cfg, job, auto=auto_salvage))
            continue
        if kind != "retryable-clean":
            actions.append({"job_id": job.get("id"), "status": "skipped", "reason": classification.get("reason") or kind})
            continue
        actions.append(retry_failed_job(cfg, job))
    report["actions"] = actions
    return actions


def format_watch_report(report: dict[str, Any]) -> str:
    counts = report.get("counts") or {}
    lines = [
        "Tasklane: action needed" if report.get("health") != "ok" else "Tasklane: ok",
        f"Status: {counts.get('needs-human', 0)} human decision(s), {counts.get('running', 0)} running, {counts.get('failed', 0)} failed",
    ]
    running = report.get("running") or []
    if running:
        lines.append("")
        lines.append("Current:")
        for job in running[:3]:
            runtime = job.get("runtime_minutes")
            runtime_text = f", {runtime} min" if runtime is not None else ""
            lines.append(f"- {job.get('id')} {job.get('project') or ''}: {job.get('title') or ''}{runtime_text}")
    gate_attention = report.get("gate_attention") or []
    if gate_attention:
        lines.append("")
        lines.append("What you need to do:")
        for index, gate in enumerate(gate_attention[:6], start=1):
            lines.append(f"{index}. {gate.get('task_uid')}")
            if gate.get("pr_url"):
                lines.append(f"   PR: {gate.get('pr_url')}")
            if gate.get("why"):
                lines.append(f"   Why: {gate.get('why')}")
            lines.append(f"   Do: {gate.get('operator_action')}")
    notification_config = report.get("notification_config") or {}
    if notification_config and not notification_config.get("configured"):
        lines.append("")
        lines.append("Notifications:")
        lines.append("- Hermes/project-chat delivery is not fully configured.")
    problems = report.get("problems") or []
    non_gate_problems = [problem for problem in problems if problem.get("code") not in {"review-gate-needs-human", "merge-gate-needs-human"}]
    if non_gate_problems:
        lines.append("")
        lines.append("Other findings:")
        for problem in non_gate_problems[:8]:
            job = problem.get("job") or {}
            suffix = f" ({job.get('id')})" if job.get("id") else ""
            lines.append(f"- {problem.get('severity')}: {problem.get('code')}: {problem.get('message')}{suffix}")
    actions = report.get("actions") or []
    if actions:
        lines.append("")
        lines.append("Actions:")
        for action in actions[:12]:
            lines.append(f"- {action.get('job_id')}: {action.get('status')} {action.get('reason') or ''}".rstrip())
    notices = report.get("notices") or []
    if notices:
        lines.append("")
        lines.append("Notices:")
        for notice in notices[:8]:
            job = notice.get("job") or {}
            lines.append(f"- {notice.get('code')}: {job.get('id')}")
    return "\n".join(lines)


def load_simple_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values


def hermes_telegram_script_chat_id(target: str, env_values: dict[str, str]) -> str | None:
    target = target.strip()
    if target == "telegram":
        return env_values.get("TELEGRAM_HOME_CHANNEL")
    match = re.fullmatch(r"telegram:(-?\d+)(?::\d+)?", target)
    if match:
        return match.group(1)
    return None


def send_hermes_script_notification(cfg: Config, text: str, *, relay: dict[str, Any], env_values: dict[str, str]) -> dict[str, Any]:
    target = str(relay.get("target") or "")
    chat_id = hermes_telegram_script_chat_id(target, env_values)
    script_path = cfg.hermes_home / "scripts" / "send_telegram_message.py"
    if not chat_id:
        return {"status": "skipped", "reason": "hermes-script-target-not-supported", "target": target}
    if not script_path.exists():
        return {"status": "skipped", "reason": "hermes-script-missing", "script": str(script_path)}
    proc = subprocess.run(
        [str(script_path), chat_id, text[:3900]],
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    raw = (proc.stdout or "").strip()
    try:
        payload: Any = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"raw": raw}
    if proc.returncode != 0:
        return {"status": "failed", "provider": "hermes-script", "target": target, "exit_code": proc.returncode, "stderr": command_output_tail(proc.stderr or ""), "response": payload}
    return {"status": "sent", "provider": "hermes-script", "target": target, "response": payload}


def send_hermes_notification(cfg: Config, text: str) -> dict[str, Any]:
    relay = hermes_notification_config(cfg)
    if not relay.get("configured"):
        return {"status": "skipped", "reason": "hermes-relay-not-configured", "relay": relay}
    env_values = load_simple_env_file(Path(str(relay["env_path"])))
    script_result = send_hermes_script_notification(cfg, text, relay=relay, env_values=env_values)
    if script_result.get("status") == "sent":
        return script_result
    script = (
        "import json,sys;"
        "sys.path.insert(0, sys.argv[1]);"
        "from tools.send_message_tool import send_message_tool;"
        "print(send_message_tool({'action':'send','target':sys.argv[2],'message':sys.stdin.read()}))"
    )
    env = os.environ.copy()
    env.update(env_values)
    env["HERMES_HOME"] = str(cfg.hermes_home)
    try:
        proc = subprocess.run(
            [str(relay["python"]), "-c", script, str(relay["agent_path"]), str(relay["target"])],
            input=text[:3900],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {"status": "failed", "provider": "hermes", "target": relay["target"], "reason": "timeout", "script_fallback": script_result, "stderr": command_output_tail(str(exc.stderr or ""))}
    raw = (proc.stdout or "").strip()
    payload: Any
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"raw": raw}
    if proc.returncode != 0:
        return {"status": "failed", "provider": "hermes", "target": relay["target"], "exit_code": proc.returncode, "stderr": command_output_tail(proc.stderr or ""), "response": payload, "script_fallback": script_result}
    if isinstance(payload, dict) and payload.get("error"):
        return {"status": "failed", "provider": "hermes", "target": relay["target"], "error": payload.get("error"), "response": payload, "script_fallback": script_result}
    return {"status": "sent", "provider": "hermes", "target": relay["target"], "response": payload}


def send_telegram_notification(cfg: Config, text: str) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or str(cfg.watch.get("telegram_bot_token") or "")
    chat_id = str(cfg.watch.get("telegram_chat_id") or cfg.default_chat_id or "")
    thread_id = str(cfg.watch.get("telegram_thread_id") or cfg.default_thread_id or "")
    if not token or not chat_id:
        return {"status": "skipped", "reason": "missing-telegram-token-or-chat-id"}
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": True}
    if thread_id:
        payload["message_thread_id"] = thread_id
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "hermes-tasklane"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        body = response.read().decode("utf-8")
    return {"status": "sent", "response": json.loads(body) if body.strip() else None}


def send_tasklane_notification(cfg: Config, text: str) -> dict[str, Any]:
    provider = str(cfg.watch.get("notification_provider") or "hermes").strip().lower()
    if provider in {"hermes", "auto"}:
        hermes_result = send_hermes_notification(cfg, text)
        if hermes_result.get("status") == "sent" or provider == "hermes":
            return hermes_result
        telegram_result = send_telegram_notification(cfg, text)
        return {"status": telegram_result.get("status"), "provider": "auto", "hermes": hermes_result, "telegram": telegram_result}
    if provider == "telegram":
        return {"provider": "telegram", **send_telegram_notification(cfg, text)}
    return {"status": "skipped", "reason": f"unknown-notification-provider:{provider}"}


def notification_state_path(cfg: Config) -> Path:
    return cfg.task_root / "notification-state.json"


def notification_cooldown_minutes(cfg: Config) -> int:
    try:
        return max(0, int(cfg.watch.get("notification_cooldown_minutes") or 240))
    except (TypeError, ValueError):
        return 240


def notification_fingerprint(payload: dict[str, Any]) -> str:
    return sha_id(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def watch_notification_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "format_version": "action-v2",
        "health": report.get("health"),
        "gate_attention": [
            {
                "task_uid": item.get("task_uid"),
                "gate": item.get("gate"),
                "reason": item.get("reason"),
                "job_id": item.get("job_id"),
            }
            for item in report.get("gate_attention") or []
        ],
        "problems": [
            {
                "severity": item.get("severity"),
                "code": item.get("code"),
                "job_id": ((item.get("job") or {}).get("id")),
                "message": item.get("message"),
            }
            for item in report.get("problems") or []
        ],
    }


def maybe_send_tasklane_notification(cfg: Config, *, channel: str, text: str, payload: dict[str, Any]) -> dict[str, Any]:
    cooldown = notification_cooldown_minutes(cfg)
    fingerprint = notification_fingerprint(payload)
    state = load_json(notification_state_path(cfg))
    if not isinstance(state, dict):
        state = {}
    entry = state.get(channel) if isinstance(state.get(channel), dict) else {}
    last_sent_at = entry.get("last_sent_at")
    elapsed = minutes_since(last_sent_at)
    if entry.get("fingerprint") == fingerprint and elapsed is not None and elapsed < cooldown:
        return {
            "status": "skipped",
            "reason": "duplicate-notification-cooldown",
            "channel": channel,
            "fingerprint": fingerprint,
            "last_sent_at": last_sent_at,
            "cooldown_minutes": cooldown,
            "remaining_minutes": cooldown - elapsed,
        }
    result = send_tasklane_notification(cfg, text)
    if result.get("status") == "sent":
        state[channel] = {
            "fingerprint": fingerprint,
            "last_sent_at": now_iso(),
            "cooldown_minutes": cooldown,
        }
        atomic_write_json(notification_state_path(cfg), state)
    result["channel"] = channel
    result["fingerprint"] = fingerprint
    return result


def dependency_summaries(cfg: Config, job: dict[str, Any], completed: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dep_id in job_dependencies(job):
        dep = find_job_record(cfg, dep_id)
        rows.append({
            "job_id": dep_id,
            "found": isinstance(dep, dict),
            "completed": dep_id in completed,
            "state": dep.get("state") if isinstance(dep, dict) else None,
            "derived_state": job_liveness_summary(dep, completed_ids=completed).get("derived_state") if isinstance(dep, dict) else "unknown",
        })
    return rows


def recommended_action_for_liveness(liveness: dict[str, Any]) -> str:
    derived = str(liveness.get("derived_state") or "unknown")
    if derived == "running-alive":
        return "No action; worker claimant is alive."
    if derived == "dead-claimant":
        return "Run guarded watch or recover the dead claimant." if liveness.get("recovery_eligible") else "Re-check shortly before recovery."
    if derived == "running-unknown-claimant":
        return "Inspect claimant metadata; Tasklane cannot verify this worker process."
    if derived == "waiting-on-dependency":
        waiting = ", ".join(liveness.get("waiting_for") or [])
        return f"Wait for dependency {waiting}." if waiting else "Wait for dependencies to complete."
    if derived == "ready":
        return "No manual action; job is ready for the gateway."
    if derived == "blocked":
        return "Inspect blocker and decide whether to retry, salvage, or stop."
    if derived == "needs-human":
        return "Human decision required; inspect prompt/blocker details."
    if derived == "failed":
        return "Review failure and use salvage/retry only if safe."
    if derived == "completed":
        return "No action; job is completed."
    return "Inspect job record and events."


def build_job_inspection(cfg: Config, job_id: str, *, events_limit: int = 5) -> dict[str, Any]:
    job = find_job_record(cfg, job_id)
    if not isinstance(job, dict):
        raise KeyError(job_id)
    completed = completed_job_ids(cfg)
    liveness = job_liveness_summary(job, completed_ids=completed)
    summary = operator_job_summary(job, completed_ids=completed)
    events = read_job_events(cfg, job_id)
    if events_limit >= 0:
        events = events[-events_limit:]
    spec = job.get("spec") or {}
    branch = spec.get("branch") or {}
    lane_plan = lane_plan_lookup(cfg, job_id)
    return {
        "job": summary,
        "liveness": liveness,
        "dependencies": dependency_summaries(cfg, job, completed),
        "events": events,
        "pr_status": pr_visibility_status(cfg, job),
        "branch": {
            "mode": branch.get("mode"),
            "base_branch": branch.get("base_branch"),
            "work_branch": branch.get("work_branch"),
            "pr_target": branch.get("pr_target"),
            "delivery_mode": spec.get("delivery_mode"),
        },
        "lane_plan": lane_plan,
        "recommended_action": recommended_action_for_liveness(liveness),
    }


def format_job_inspection(report: dict[str, Any]) -> str:
    job = report.get("job") or {}
    live = report.get("liveness") or {}
    pr = report.get("pr_status") or {}
    branch = report.get("branch") or {}
    lines = [
        f"Tasklane job {job.get('id')}",
        f"State: {job.get('state')} / {live.get('derived_state')}",
        f"Project: {job.get('project') or ''}",
        f"Title: {job.get('title') or ''}",
        f"Branch: {branch.get('work_branch') or '-'} -> {branch.get('pr_target') or branch.get('base_branch') or '-'} ({branch.get('delivery_mode') or '-'})",
        f"PR: {pr.get('status')} {pr.get('url') or pr.get('message') or ''}".rstrip(),
    ]
    if live.get("claimed_by"):
        lines.append(f"Claimant: {live.get('claimed_by')} pid={live.get('claimant_pid')} alive={live.get('claimant_alive')}")
    if live.get("waiting_for"):
        lines.append("Waiting for: " + ", ".join(live.get("waiting_for") or []))
    deps = report.get("dependencies") or []
    if deps:
        lines.append("Dependencies:")
        for dep in deps:
            lines.append(f"- {dep.get('job_id')}: {dep.get('state') or 'missing'} / {dep.get('derived_state')} completed={dep.get('completed')}")
    lane = report.get("lane_plan") or {}
    if lane:
        lane_data = lane.get("lane") or {}
        lines.append(f"Lane: {lane.get('wave_id')} / {lane_data.get('lane_id')} ({lane_data.get('delivery_group')})")
    events = report.get("events") or []
    if events:
        lines.append("Recent events:")
        for event in events:
            lines.append(f"- {event.get('timestamp')}: {event.get('event_type') or event.get('type')} {event.get('state') or ''}".rstrip())
    lines.append(f"Recommended action: {report.get('recommended_action')}")
    return "\n".join(lines)


def command_inspect(cfg: Config, job_id: str, *, json_output: bool, events_limit: int) -> int:
    report = build_job_inspection(cfg, job_id, events_limit=events_limit)
    if json_output:
        print(json.dumps(report, indent=2))
    else:
        print(format_job_inspection(report))
    return 0


def command_watch(
    cfg: Config,
    *,
    mode: str,
    stale_minutes: int | None,
    expected_base_values: list[str] | None,
    ignored_blocked_values: list[str] | None,
    notify: bool,
    quiet_ok: bool,
    json_output: bool,
    fail_on_problems: bool,
) -> int:
    expected = watch_expected_base_map(cfg, expected_base_values)
    ignored = watch_ignored_blocked_jobs(cfg, ignored_blocked_values)
    report = build_watch_report(
        cfg,
        mode=mode,
        stale_running_minutes=stale_minutes,
        expected_base=expected,
        ignored_blocked=ignored,
        check_gateway=True,
    )
    if mode == "guarded":
        apply_guarded_watch_actions(cfg, report)
        report = {
            **build_watch_report(
                cfg,
                mode=mode,
                stale_running_minutes=stale_minutes,
                expected_base=expected,
                ignored_blocked=ignored,
                check_gateway=True,
            ),
            "actions": report.get("actions") or [],
        }
    text = format_watch_report(report)
    if notify and (not quiet_ok or report.get("health") != "ok"):
        try:
            report["notification"] = maybe_send_tasklane_notification(
                cfg,
                channel="watch",
                text=text,
                payload=watch_notification_payload(report),
            )
        except Exception as exc:
            report["notification"] = {"status": "failed", "error": str(exc)}
    if json_output:
        if quiet_ok and report.get("health") == "ok":
            return 0
        print(json.dumps(report, indent=2))
    elif not quiet_ok or report.get("health") != "ok":
        print(text)
        if notify and report.get("notification"):
            notification = report.get("notification") or {}
            print("")
            print(f"Notification: {notification.get('status')} {notification.get('reason') or notification.get('error') or ''}".rstrip())
    if fail_on_problems and report.get("health") != "ok":
        return 2
    return 0


def command_salvage(cfg: Config, job_id: str, *, auto: bool, verify: bool) -> int:
    ensure_layout(cfg)
    job = find_job_record(cfg, job_id)
    if not isinstance(job, dict):
        print(json.dumps({"job_id": job_id, "status": "missing-job"}, indent=2))
        return 2
    classification = classify_failed_job(cfg, job, int(cfg.watch.get("max_retry_attempts") or 3))
    result: dict[str, Any] = {"job_id": job_id, **classification}
    if classification.get("classification") == "salvage-needed":
        if auto:
            result = auto_salvage_failed_job(cfg, job, auto=True)
        elif verify:
            inspection = classification.get("inspection") or {}
            if inspection.get("ok") and inspection.get("worktree_path"):
                checks = run_delivery_checks(cfg, job, Path(str(inspection["worktree_path"])), inspection)
                result["bootstrap"] = checks.get("bootstrap") or []
                result["verification"] = checks.get("verification") or []
                result["status"] = "verification-passed" if checks.get("ok") else str(checks.get("reason") or "verification-failed")
        else:
            result["status"] = "salvage-needed"
    print(json.dumps(result, indent=2))
    if result.get("status") in {"salvaged", "verification-passed", "salvage-needed"}:
        return 0
    return 2


def command_status(cfg: Config) -> int:
    ensure_layout(cfg)
    state = load_state(cfg)
    submitted = dict(state.get("submitted") or {})
    active_runs: list[dict[str, Any]] = []
    blocked_runs: list[dict[str, Any]] = []
    active_jobs: list[dict[str, Any]] = []
    waiting_jobs: list[dict[str, Any]] = []
    blocked_jobs: list[dict[str, Any]] = []
    completed = completed_job_ids(cfg)
    for path in cfg.runs_dir.glob("*.json"):
        payload = load_json(path)
        if not isinstance(payload, dict) or payload.get("kind") != "coding_task":
            continue
        item = {
            "id": payload.get("id"),
            "state": payload.get("state"),
            "stage": ((payload.get("workflow") or {}).get("current_stage")),
            "repo_key": ((payload.get("repo") or {}).get("key")),
            "blocked_reason": payload.get("blocked_reason"),
        }
        if item["state"] in {"queued", "running"}:
            active_runs.append(item)
        if item["state"] == "blocked":
            blocked_runs.append(item)
    for payload in iter_job_records(cfg, {"ready", "running", "blocked", "needs-human"}):
        state = str(payload.get("state") or "")
        if state == "ready":
            missing = waiting_dependencies(payload, completed)
            if missing:
                waiting_jobs.append(operator_job_summary(payload, state="waiting", waiting_for=missing, completed_ids=completed))
                continue
            active_jobs.append(operator_job_summary(payload, completed_ids=completed))
        elif state == "running":
            active_jobs.append(operator_job_summary(payload, completed_ids=completed))
        elif state in {"blocked", "needs-human"}:
            blocked_jobs.append(operator_job_summary(payload, completed_ids=completed))
    gate_attention = submitted_gate_attention(cfg)
    report = {
        "inbox": len([p for p in cfg.inbox_dir.iterdir() if p.is_file()]) if cfg.inbox_dir.exists() else 0,
        "submitted": len(submitted),
        "completed": len([p for p in cfg.completed_dir.iterdir() if p.is_file() and p.suffix in TASK_SUFFIXES]) if cfg.completed_dir.exists() else 0,
        "failed": len([p for p in cfg.failed_dir.iterdir() if p.is_file() and p.suffix in TASK_SUFFIXES]) if cfg.failed_dir.exists() else 0,
        "cancelled": len([p for p in cfg.cancelled_dir.iterdir() if p.is_file() and p.suffix in TASK_SUFFIXES]) if cfg.cancelled_dir.exists() else 0,
        "active_jobs": active_jobs,
        "waiting_jobs": waiting_jobs,
        "blocked_jobs": blocked_jobs,
        "gate_attention": gate_attention,
        "active_runs": active_runs,
        "blocked_runs": blocked_runs,
    }
    print(json.dumps(report, indent=2))
    return 0


def command_init(cfg: Config, config_path_override: str | None = None) -> int:
    ensure_layout(cfg)
    config_path = Path(config_path_override).expanduser() if config_path_override else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        atomic_write_json(
            config_path,
            {
                "hermes_home": str(cfg.hermes_home),
                "task_root": str(cfg.task_root),
                "poll_repo_idle": True,
                "max_pending_per_repo": 1,
                "github_owner_hint": cfg.github_owner_hint,
                "default_platform": cfg.default_platform,
                "default_chat_id": cfg.default_chat_id,
                "default_thread_id": cfg.default_thread_id,
                "review_gate": {"enabled": False},
                "wave_planner": {
                    "max_active_prs": 3,
                    "branch_prefix": "tasklane/",
                    "max_issues_per_wave": 10,
                    "issue_scan_limit": 50,
                    "issue_include_terms": [],
                    "issue_exclude_terms": [],
                    "issue_labels_any": [],
                    "issue_labels_all": [],
                    "issue_milestone": "",
                    "max_pr_lanes": 3,
                    "review_loops": 2,
                    "max_active_contract_prs": 1,
                    "max_active_large_feature_prs": 1,
                    "max_active_docs_prs": 2,
                    "merge_gate": False,
                    "auto_merge": False,
                    "projects": {},
                },
                "watch": {
                    "mode": "observe",
                    "notification_provider": "hermes",
                    "hermes_target": "telegram",
                    "notification_cooldown_minutes": 240,
                    "auto_salvage": False,
                    "baseline_verification": False,
                    "allow_matching_baseline_failures": False,
                    "bootstrap_timeout_seconds": 1800,
                    "verification_timeout_seconds": 1800,
                    "stale_running_minutes": 180,
                    "max_retry_attempts": 3,
                    "bootstrap_commands": [],
                    "bootstrap_profiles": {},
                    "verification_commands": ["git diff --check"],
                    "verification_profiles": {},
                    "allow_task_command_overrides": False,
                    "expected_base_branches": {},
                    "ignored_blocked_jobs": [],
                },
            },
        )
    examples_dir = cfg.task_root / "examples"
    examples_dir.mkdir(parents=True, exist_ok=True)
    example = examples_dir / "example-task.md"
    if not example.exists():
        example.write_text(
            "---\nrepo_path: /absolute/path/to/repo\nbase_branch: main\nbranch_mode: new-branch\ndelivery_mode: pull-request\nrequest_type: task-small\nplatform: telegram\nchat_id: -1001234567890\nproject: Example\nallowed_paths: README.md\nallow_unlisted_paths: false\n---\nImplement the task here and run the strongest relevant verification before opening a PR.\n",
            encoding="utf-8",
        )
    print(json.dumps({"config": str(config_path), "task_root": str(cfg.task_root), "example_task": str(example)}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="File-based task inbox for Hermes governed runs")
    parser.add_argument("--config", help="Path to config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create config and task folders")
    sub.add_parser("doctor", help="Check local setup")
    sub.add_parser("sync", help="Convert inbox files into Hermes queue items")
    sub.add_parser("reconcile", help="Reconcile submitted tasks from governed run state")
    sub.add_parser("status", help="Show tasklane and run status")
    inspect = sub.add_parser("inspect", help="Inspect one job with liveness, dependency, PR, and event context")
    inspect.add_argument("job_id", help="Hermes JobStore job id")
    inspect.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    inspect.add_argument("--events", type=int, default=5, help="Number of recent job events to include")
    plan_wave = sub.add_parser("plan-wave", help="Dry-run a grouped issue wave without queueing jobs")
    plan_wave.add_argument("--repo", required=True, help="Local repository path to inspect")
    plan_wave.add_argument("--project", required=True, help="Project label used in reports")
    plan_wave.add_argument("--base", default="development", help="Target base branch for proposed lanes")
    plan_wave.add_argument("--max-active-prs", type=int, help="Override active tasklane PR cap")
    plan_wave.add_argument("--branch-prefix", help="Override tasklane branch prefix used for PR counting/proposals")
    plan_wave.add_argument("--issue-limit", type=int, help="Maximum in-scope GitHub issues to select for the wave")
    plan_wave.add_argument("--issue-scan-limit", type=int, help="Maximum open GitHub issues to scan before scope filtering")
    plan_wave.add_argument("--issue-include", action="append", default=None, help="Only include issues whose title, body, label, milestone, or #number contains this term; repeatable")
    plan_wave.add_argument("--issue-exclude", action="append", default=None, help="Exclude issues whose title, body, label, milestone, or #number contains this term; repeatable")
    plan_wave.add_argument("--issue-label", action="append", default=None, help="Only include issues with any of these labels; repeatable")
    plan_wave.add_argument("--issue-label-all", action="append", default=None, help="Only include issues with every listed label; repeatable")
    plan_wave.add_argument("--issue-milestone", help="Only include issues from this milestone")
    plan_wave.add_argument("--max-lanes", type=int, help="Maximum proposed PR lanes")
    plan_wave.add_argument("--enqueue", action="store_true", help="Create and sync guarded task files for the proposed lanes")
    plan_wave.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    wave_runner = sub.add_parser("wave-runner", help="Run one guarded rolling wave cycle for a project")
    wave_runner.add_argument("--repo", required=True, help="Local repository path to inspect")
    wave_runner.add_argument("--project", required=True, help="Project label used in reports")
    wave_runner.add_argument("--base", default="development", help="Target base branch for proposed lanes")
    wave_runner.add_argument("--enqueue", action="store_true", help="Create and sync task files when the project has free PR slots")
    wave_runner.add_argument("--notify", action="store_true", help="Send Telegram only when a blocker or human decision exists")
    wave_runner.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    salvage = sub.add_parser("salvage", help="Inspect or auto-deliver a failed job with worktree changes")
    salvage.add_argument("job_id", help="Failed Hermes job ID to inspect or salvage")
    salvage.add_argument("--verify", action="store_true", help="Run configured verification commands without committing or pushing")
    salvage.add_argument("--auto", action="store_true", help="Run verification, commit, push, open PR, and mark the task completed when safe")
    watch = sub.add_parser("watch", help="Review queue health and optionally apply guarded recovery")
    watch.add_argument("--mode", choices=["observe", "guarded"], default=None, help="observe reports only; guarded retries narrowly safe transient failures")
    watch.add_argument("--stale-minutes", type=int, help="Warn when a running job exceeds this age")
    watch.add_argument("--expected-base", action="append", default=[], metavar="PROJECT_OR_REPO=BRANCH", help="Expected base branch policy, e.g. Alvin=develop")
    watch.add_argument("--ignore-blocked", action="append", default=[], metavar="JOB_ID", help="Blocked job ID that should be treated as an expected exception")
    watch.add_argument("--notify", action="store_true", help="Send Telegram notification using TELEGRAM_BOT_TOKEN plus configured chat ID")
    watch.add_argument("--quiet-ok", action="store_true", help="Suppress output and notifications when health is ok")
    watch.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    watch.add_argument("--fail-on-problems", action="store_true", help="Exit 2 when warnings or critical findings exist")
    dashboard = sub.add_parser("dashboard", help="Run the read-only Tasklane Pipeline web dashboard")
    dashboard.add_argument("--host", default="127.0.0.1", help="Bind address; use 0.0.0.0 for trusted internal networks")
    dashboard.add_argument("--port", type=int, default=8765, help="Bind port")
    return parser


def command_requires_lock(args: argparse.Namespace, cfg: Config) -> bool:
    if args.command in {"sync", "reconcile"}:
        return True
    if args.command == "salvage":
        return bool(getattr(args, "auto", False))
    if args.command == "watch":
        mode = getattr(args, "mode", None) or str(cfg.watch.get("mode") or "observe")
        return mode == "guarded"
    if args.command == "plan-wave":
        return bool(getattr(args, "enqueue", False))
    if args.command == "wave-runner":
        return bool(getattr(args, "enqueue", False))
    return False


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    try:
        lock_ctx = tasklane_lock(cfg) if command_requires_lock(args, cfg) else nullcontext()
        with lock_ctx:
            if args.command == "init":
                return command_init(cfg, args.config)
            if args.command == "doctor":
                return command_doctor(cfg)
            if args.command == "sync":
                return command_sync(cfg)
            if args.command == "reconcile":
                return command_reconcile(cfg)
            if args.command == "status":
                return command_status(cfg)
            if args.command == "inspect":
                return command_inspect(cfg, args.job_id, json_output=args.json, events_limit=args.events)
            if args.command == "plan-wave":
                return command_plan_wave(
                    cfg,
                    repo_path=Path(args.repo).expanduser(),
                    project=args.project,
                    base_branch=args.base,
                    max_active_prs=args.max_active_prs,
                    branch_prefix=args.branch_prefix,
                    issue_limit=args.issue_limit,
                    issue_scan_limit=args.issue_scan_limit,
                    max_lanes=args.max_lanes,
                    issue_includes=args.issue_include,
                    issue_excludes=args.issue_exclude,
                    issue_labels_any=args.issue_label,
                    issue_labels_all=args.issue_label_all,
                    issue_milestone=args.issue_milestone,
                    enqueue=args.enqueue,
                    json_output=args.json,
                )
            if args.command == "wave-runner":
                return command_wave_runner(
                    cfg,
                    repo_path=Path(args.repo).expanduser(),
                    project=args.project,
                    base_branch=args.base,
                    enqueue=args.enqueue,
                    notify=args.notify or bool_from_any(cfg.watch.get("notify"), default=False),
                    json_output=args.json,
                )
            if args.command == "salvage":
                return command_salvage(cfg, args.job_id, auto=args.auto, verify=args.verify)
            if args.command == "watch":
                mode = args.mode or str(cfg.watch.get("mode") or "observe")
                if mode not in {"observe", "guarded"}:
                    raise ValueError("watch mode must be observe or guarded")
                notify = args.notify if mode == "observe" else args.notify or bool_from_any(cfg.watch.get("notify"), default=False)
                return command_watch(
                    cfg,
                    mode=mode,
                    stale_minutes=args.stale_minutes,
                    expected_base_values=args.expected_base,
                    ignored_blocked_values=args.ignore_blocked,
                    notify=notify,
                    quiet_ok=args.quiet_ok,
                    json_output=args.json,
                    fail_on_problems=args.fail_on_problems,
                )
            if args.command == "dashboard":
                from .dashboard import serve_dashboard

                serve_dashboard(cfg, host=args.host, port=args.port)
                return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
