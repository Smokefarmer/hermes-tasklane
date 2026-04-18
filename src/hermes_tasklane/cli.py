from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
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
}
ACTIVE_RUN_STATES = {"queued", "running"}
ACTIVE_JOB_STATES = {"ready", "running"}
JOB_STATES = {"draft", "ready", "running", "blocked", "completed", "failed", "needs-human"}
TASK_SUFFIXES = {".md", ".txt"}


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


@dataclass
class Config:
    hermes_home: Path
    task_root: Path
    poll_repo_idle: bool
    max_pending_per_repo: int
    github_owner_hint: str | None

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
    dependencies: list[str]
    delivery_group: str | None


def load_task_file(path: Path) -> TaskFile:
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
    if not work_branch and delivery_group and branch_mode == "new-branch":
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
                "dependencies",
                "depends_on",
                "delivery_group",
                "pr_group",
                "epic",
            }
        },
        platform=meta.get("platform"),
        chat_id=meta.get("chat_id"),
        thread_id=meta.get("thread_id"),
        project=meta.get("project"),
        title=meta.get("title") or path.stem.replace("-", " ").replace("_", " ").strip() or uid,
        allowed_paths=parse_csv(meta.get("allowed_paths")),
        denied_paths=parse_csv(meta.get("denied_paths")),
        allow_unlisted_paths=parse_bool(meta.get("allow_unlisted_paths"), default=True),
        review_loops=int(meta.get("review_loops") or 3),
        security_review=parse_bool(meta.get("security_review"), default=True),
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


def task_job_id(task: TaskFile) -> str:
    return f"tasklane_{sha_id(task.uid)}"


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
            "base_branch": task.base_branch if task.branch_mode == "new-branch" else None,
            "work_branch": task.work_branch or None,
            "pr_target": task.base_branch if task.delivery_mode == "pull-request" else None,
        },
        "delivery_mode": task.delivery_mode,
        "dependencies": task.dependencies,
        "pipeline": {
            "budgets": {"review_loops": task.review_loops},
            "security_review": task.security_review,
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


def run_path(cfg: Config, run_id: str) -> Path:
    return cfg.runs_dir / f"{run_id}.json"


def event_log_path(cfg: Config, run_id: str) -> Path:
    return cfg.events_dir / f"{run_id}.jsonl"


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
            task = load_task_file(path)
        except Exception as exc:
            actions.append({"task": path.name, "status": "invalid", "error": str(exc)})
            continue
        loaded_tasks[path] = task
    uid_to_job_id = {task.uid: task_job_id(task) for task in loaded_tasks.values()}
    batch_job_ids = set(uid_to_job_id.values())
    for path, task in loaded_tasks.items():
        if task.uid in submitted:
            actions.append({"task": path.name, "status": "already-submitted", "run_id": submitted[task.uid].get("run_id")})
            continue
        expected_repo_key = repo_key(task.repo_path)
        if cfg.poll_repo_idle:
            active = active_runs_for_repo(cfg, expected_repo_key)
            active_jobs = [job for job in active_jobs_for_repo(cfg, expected_repo_key) if job.get("id") not in batch_job_ids]
            if active:
                actions.append({"task": path.name, "status": "deferred", "reason": "repo-active-run", "run_ids": [item.get("id") for item in active]})
                continue
            if active_jobs:
                actions.append({"task": path.name, "status": "deferred", "reason": "repo-active-job", "job_ids": [item.get("id") for item in active_jobs]})
                continue
            if repo_lock_exists(cfg, expected_repo_key):
                actions.append({"task": path.name, "status": "deferred", "reason": "repo-lock-active"})
                continue
        job_id = uid_to_job_id[task.uid]
        task.dependencies = dependency_job_ids(task, uid_to_job_id)
        record = job_record(task, job_id)
        ready_path = job_path(cfg, job_id, "ready")
        if find_job_record(cfg, job_id):
            actions.append({"task": path.name, "status": "already-job-recorded", "job_id": job_id})
            continue
        atomic_write_json(ready_path, record)
        append_jsonl(job_event_log_path(cfg, job_id), {"timestamp": now_iso(), "job_id": job_id, "event_type": "job_created", "state": "ready", "reason": "tasklane-sync", "metadata": {"source_file": str(path)}})
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
        actions.append({"task": path.name, "status": "job-ready", "job_id": job_id, "job_file": str(ready_path)})
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


def ci_status(owner: str, repo: str, sha: str) -> dict[str, Any]:
    combined = github_get(f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/status") or {}
    suites = github_get(f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-suites") or {}
    lines: list[str] = []
    pending = False
    failed = False
    suite_rows: list[dict[str, Any]] = []
    for suite in suites.get("check_suites", []) or []:
        app = ((suite.get("app") or {}).get("slug") or (suite.get("app") or {}).get("name") or "unknown")
        status = suite.get("status")
        conclusion = suite.get("conclusion")
        suite_rows.append({"app": app, "status": status, "conclusion": conclusion})
        lines.append(f"suite {app}: {status}/{conclusion or 'pending'}")
        if status != "completed" or not conclusion:
            pending = True
        elif str(conclusion).lower() in {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}:
            failed = True
    overall = str(combined.get("state") or "").lower()
    if overall in {"failure", "error"}:
        failed = True
    elif overall == "pending":
        pending = True
    return {
        "status": "fail" if failed else "pending" if pending else "pass",
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
    source_path = Path(entry["source_path"])
    if source_path.exists():
        moved = move_task_file(source_path, destination_dir)
        note_path = moved.with_suffix(moved.suffix + ".result.json")
        atomic_write_json(note_path, note)


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
                result = dict(job_payload.get("result") or {})
                finalize_submitted_task(cfg, task_uid, entry, cfg.completed_dir, {"job_id": job_id, "state": job_state, "result": result})
                actions.append({"task_uid": task_uid, "status": "completed", "job_id": job_id})
                continue
            if job_state == "failed":
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


def command_status(cfg: Config) -> int:
    ensure_layout(cfg)
    state = load_state(cfg)
    submitted = dict(state.get("submitted") or {})
    active_runs: list[dict[str, Any]] = []
    blocked_runs: list[dict[str, Any]] = []
    active_jobs: list[dict[str, Any]] = []
    blocked_jobs: list[dict[str, Any]] = []
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
        spec = payload.get("spec") or {}
        item = {
            "id": payload.get("id"),
            "state": payload.get("state"),
            "repo_key": ((spec.get("repo") or {}).get("key")),
            "project": spec.get("project"),
            "title": ((spec.get("request") or {}).get("title")),
            "error": payload.get("last_error"),
        }
        if item["state"] in {"ready", "running"}:
            active_jobs.append(item)
        if item["state"] in {"blocked", "needs-human"}:
            blocked_jobs.append(item)
    report = {
        "inbox": len([p for p in cfg.inbox_dir.iterdir() if p.is_file()]) if cfg.inbox_dir.exists() else 0,
        "submitted": len(submitted),
        "completed": len([p for p in cfg.completed_dir.iterdir() if p.is_file() and p.suffix in TASK_SUFFIXES]) if cfg.completed_dir.exists() else 0,
        "failed": len([p for p in cfg.failed_dir.iterdir() if p.is_file() and p.suffix in TASK_SUFFIXES]) if cfg.failed_dir.exists() else 0,
        "cancelled": len([p for p in cfg.cancelled_dir.iterdir() if p.is_file() and p.suffix in TASK_SUFFIXES]) if cfg.cancelled_dir.exists() else 0,
        "active_jobs": active_jobs,
        "blocked_jobs": blocked_jobs,
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    try:
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
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
