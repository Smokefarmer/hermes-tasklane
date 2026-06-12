"""Git worktree isolation, job-prompt assembly, and delivery validation.

Ported from Hermes's ``GatewayRunner`` job methods (gateway/run.py) into standalone
module functions. Each coding job runs in an isolated ``git worktree`` so concurrent
jobs and the user's own checkout never collide.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from tasklane.paths import worktrees_root

logger = logging.getLogger("tasklane.worktree")

_GIT_TIMEOUT = 30


class WorktreePreparationError(RuntimeError):
    """Raised when an isolated job worktree cannot be prepared."""


def run_git(args: List[str], *, cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, stdout=exc.stdout or "", stderr=str(exc))
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, stdout="", stderr=str(exc))


def _worktree_isolation_enabled() -> bool:
    return str(os.getenv("TASKLANE_JOB_WORKTREE_ISOLATION", "1")).strip().lower() not in {"0", "false", "no", "off"}


def git_root(repo_path: Path) -> Path:
    if not repo_path.exists():
        raise WorktreePreparationError(f"repo path does not exist: {repo_path}")
    proc = run_git(["git", "rev-parse", "--show-toplevel"], cwd=repo_path)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise WorktreePreparationError(f"repo path is not a git worktree: {repo_path}")
    return Path(proc.stdout.strip()).resolve()


def _ref_exists(repo_root: Path, ref: str) -> bool:
    proc = run_git(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo_root)
    return proc.returncode == 0


def _resolve_base_ref(repo_root: Path, base_branch: str) -> str:
    if not base_branch:
        return "HEAD"
    candidates = [base_branch]
    if not base_branch.startswith("refs/") and not base_branch.startswith("origin/"):
        candidates = [f"origin/{base_branch}", base_branch]
    for candidate in candidates:
        if _ref_exists(repo_root, candidate):
            return candidate
    raise WorktreePreparationError(f"base branch/ref does not exist: {base_branch}")


def _free_checked_out_branch(*, repo_root: Path, branch_name: str, fallback_ref: str) -> None:
    proc = run_git(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    if proc.returncode != 0:
        raise WorktreePreparationError(f"failed to inspect worktrees: {proc.stderr.strip()}")
    current_path: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line.removeprefix("worktree ").strip()
            continue
        if line.strip() == f"branch refs/heads/{branch_name}" and current_path:
            checked_out_path = Path(current_path).resolve()
            if checked_out_path != repo_root.resolve():
                raise WorktreePreparationError(f"work_branch already checked out in another worktree: {checked_out_path}")
            status = run_git(["git", "status", "--porcelain"], cwd=repo_root)
            if status.returncode != 0:
                raise WorktreePreparationError(f"failed to inspect current checkout: {status.stderr.strip()}")
            if status.stdout.strip():
                raise WorktreePreparationError(f"work_branch checked out in main repo and worktree is dirty: {branch_name}")
            detach = run_git(["git", "checkout", "--detach", fallback_ref], cwd=repo_root)
            if detach.returncode != 0:
                raise WorktreePreparationError(f"failed to detach main checkout from {branch_name}: {detach.stderr.strip()}")
            return


def _copy_worktree_includes(*, repo_root: Path, worktree_path: Path) -> None:
    include_file = repo_root / ".worktreeinclude"
    if not include_file.exists():
        return
    repo_root_resolved = repo_root.resolve()
    worktree_path_resolved = worktree_path.resolve()
    for line in include_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        src = repo_root / entry
        dst = worktree_path / entry
        try:
            src_resolved = src.resolve(strict=False)
            dst_resolved = dst.resolve(strict=False)
        except (OSError, ValueError):
            continue
        if repo_root_resolved not in (src_resolved, *src_resolved.parents):
            logger.warning("Skipping .worktreeinclude entry outside repo root: %s", entry)
            continue
        if worktree_path_resolved not in (dst_resolved, *dst_resolved.parents):
            logger.warning("Skipping .worktreeinclude entry that escapes worktree: %s", entry)
            continue
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
        elif src.is_dir() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(str(src_resolved), str(dst))


def remove_stale_worktree(*, repo_root: Path, worktree_path: Path) -> None:
    if not worktree_path.exists():
        return
    proc = run_git(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo_root)
    if proc.returncode != 0:
        shutil.rmtree(worktree_path, ignore_errors=True)


def _find_reusable_worktree(
    *,
    repo_root: Path,
    worktree_root: Path,
    safe_job: str,
    worktree_path: Path,
    branch: Dict[str, Any],
    delivery_mode: str,
) -> Dict[str, Any] | None:
    mode = str(branch.get("mode") or "").strip().lower()
    work_branch = str(branch.get("work_branch") or "").strip()
    candidates = sorted(
        (p for p in worktree_root.glob(f"{safe_job}-a*") if p != worktree_path and p.exists()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        root = run_git(["git", "rev-parse", "--show-toplevel"], cwd=candidate)
        if root.returncode != 0 or Path(root.stdout.strip()).resolve() != candidate.resolve():
            continue
        if delivery_mode == "report-only" or mode == "detached-review":
            return {"original_repo_path": str(repo_root), "worktree_path": str(candidate),
                    "branch": "", "mode": mode, "base_ref": "", "reused": True}
        if not work_branch:
            continue
        current = run_git(["git", "branch", "--show-current"], cwd=candidate)
        if current.returncode == 0 and current.stdout.strip() == work_branch:
            return {"original_repo_path": str(repo_root), "worktree_path": str(candidate),
                    "branch": work_branch, "mode": mode, "base_ref": "", "reused": True}
    return None


def _create_job_worktree(*, repo_root: Path, job_id: str, attempt: int, branch: Dict[str, Any], delivery_mode: str) -> Dict[str, Any]:
    mode = str(branch.get("mode") or "").strip().lower()
    base_branch = str(branch.get("base_branch") or "").strip()
    work_branch = str(branch.get("work_branch") or "").strip()
    safe_repo = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_root.name).strip("-._") or "repo"
    safe_job = re.sub(r"[^A-Za-z0-9._-]+", "-", job_id).strip("-._") or "job"
    worktree_root = worktrees_root() / safe_repo
    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_root / f"{safe_job}-a{attempt or 0}"
    remove_stale_worktree(repo_root=repo_root, worktree_path=worktree_path)

    base_ref = ""
    created_branch = None
    if attempt > 1:
        reusable = _find_reusable_worktree(repo_root=repo_root, worktree_root=worktree_root, safe_job=safe_job,
                                           worktree_path=worktree_path, branch=branch, delivery_mode=delivery_mode)
        if reusable:
            return reusable

    if delivery_mode == "report-only" or mode == "detached-review":
        base_ref = _resolve_base_ref(repo_root, base_branch)
        cmd = ["git", "worktree", "add", "--detach", str(worktree_path), base_ref]
    elif mode == "new-branch":
        if not work_branch:
            raise WorktreePreparationError("new-branch job requires branch.work_branch")
        if _ref_exists(repo_root, f"refs/heads/{work_branch}"):
            if attempt <= 1:
                raise WorktreePreparationError(f"new-branch job work_branch already exists: {work_branch}")
            cmd = ["git", "worktree", "add", str(worktree_path), work_branch]
        elif _ref_exists(repo_root, f"refs/remotes/origin/{work_branch}"):
            if attempt <= 1:
                raise WorktreePreparationError(f"new-branch job work_branch already exists: {work_branch}")
            cmd = ["git", "worktree", "add", "-b", work_branch, str(worktree_path), f"origin/{work_branch}"]
            created_branch = work_branch
        else:
            base_ref = _resolve_base_ref(repo_root, base_branch)
            cmd = ["git", "worktree", "add", "-b", work_branch, str(worktree_path), base_ref]
            created_branch = work_branch
    elif mode == "existing-branch":
        if not work_branch:
            raise WorktreePreparationError("existing-branch job requires branch.work_branch")
        _free_checked_out_branch(repo_root=repo_root, branch_name=work_branch,
                                 fallback_ref=_resolve_base_ref(repo_root, base_branch))
        if _ref_exists(repo_root, f"refs/heads/{work_branch}"):
            cmd = ["git", "worktree", "add", str(worktree_path), work_branch]
        elif _ref_exists(repo_root, f"refs/remotes/origin/{work_branch}"):
            cmd = ["git", "worktree", "add", "-b", work_branch, str(worktree_path), f"origin/{work_branch}"]
            created_branch = work_branch
        else:
            raise WorktreePreparationError(f"existing-branch work_branch not found locally or on origin: {work_branch}")
    else:
        raise WorktreePreparationError(f"unsupported branch mode for worktree isolation: {mode or '(empty)'}")

    proc = run_git(cmd, cwd=repo_root)
    if proc.returncode != 0:
        remove_stale_worktree(repo_root=repo_root, worktree_path=worktree_path)
        raise WorktreePreparationError(f"failed to create isolated worktree: {proc.stderr.strip() or proc.stdout.strip()}")
    _copy_worktree_includes(repo_root=repo_root, worktree_path=worktree_path)
    return {"original_repo_path": str(repo_root), "worktree_path": str(worktree_path),
            "branch": created_branch or work_branch, "mode": mode, "base_ref": base_ref}


def prepare_job_workspace(record: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any] | None]:
    """Return (prepared_record, worktree_info). worktree_info is None if isolation off."""
    if not _worktree_isolation_enabled():
        return record, None
    spec = dict(record.get("spec") or {})
    repo = dict(spec.get("repo") or {})
    raw_repo_path = str(repo.get("path") or "").strip()
    if not raw_repo_path:
        raise WorktreePreparationError("job repo.path is required for worktree isolation")
    repo_root = git_root(Path(raw_repo_path).expanduser())
    branch = dict(spec.get("branch") or {})
    delivery_mode = str(spec.get("delivery_mode") or "").strip().lower()
    mode = str(branch.get("mode") or "").strip().lower()
    worktree_info = _create_job_worktree(repo_root=repo_root, job_id=str(record.get("id") or "job"),
                                         attempt=int(record.get("attempt") or 0), branch=branch, delivery_mode=delivery_mode)

    prepared = copy.deepcopy(record)
    prepared_spec = dict(prepared.get("spec") or {})
    prepared_repo = dict(prepared_spec.get("repo") or {})
    prepared_repo["path"] = worktree_info["worktree_path"]
    prepared_spec["repo"] = prepared_repo
    metadata = dict(prepared_spec.get("metadata") or {})
    metadata["workspace"] = {
        "isolated": True,
        "original_repo_path": str(repo_root),
        "worktree_path": worktree_info["worktree_path"],
        "mode": mode,
        "base_ref": worktree_info.get("base_ref"),
    }
    prepared_spec["metadata"] = metadata
    prepared["spec"] = prepared_spec
    return prepared, worktree_info


def cleanup_worktree(worktree_info: Dict[str, Any] | None, *, keep: bool) -> None:
    if not worktree_info or keep:
        return
    repo_root = Path(str(worktree_info.get("original_repo_path") or "")).expanduser()
    worktree_path = Path(str(worktree_info.get("worktree_path") or "")).expanduser()
    if not repo_root.exists() or not worktree_path.exists():
        return
    proc = run_git(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo_root)
    if proc.returncode != 0:
        logger.warning("Failed to remove job worktree %s: %s", worktree_path, proc.stderr.strip())


def job_prompt(record: Dict[str, Any]) -> str:
    spec = record.get("spec") if isinstance(record.get("spec"), dict) else {}
    request = spec.get("request") if isinstance(spec.get("request"), dict) else {}
    repo = spec.get("repo") if isinstance(spec.get("repo"), dict) else {}
    branch = spec.get("branch") if isinstance(spec.get("branch"), dict) else {}
    scope = spec.get("scope") if isinstance(spec.get("scope"), dict) else {}
    allowed_paths = scope.get("allowed_paths") or []
    denied_paths = scope.get("denied_paths") or []
    acceptance = request.get("acceptance_criteria") or []
    delivery_mode = spec.get("delivery_mode") or "pull-request"
    workspace = (spec.get("metadata") or {}).get("workspace") or {}
    lines = [
        "You are running a TaskLane autonomous coding job.",
        "",
        "Pipeline contract:",
        "1. Plan the concrete steps briefly before editing.",
        "2. Implement the requested change in the target repo.",
        "3. Review your own diff for security, correctness, and clean coding.",
        "4. Fix review findings, with up to 3 implementation/review loops if needed.",
        "5. Verify with the strongest relevant tests or checks.",
        "6. Deliver according to the delivery mode.",
        "",
        f"Job ID: {record.get('id')}",
        f"Project: {spec.get('project') or ''}",
        f"Repo path: {repo.get('path') or ''}",
        f"Request type: {request.get('type') or ''}",
        f"Title: {request.get('title') or ''}",
        f"Task:\n{request.get('body') or request.get('title') or ''}",
        "",
        "Branch contract:",
        f"- mode: {branch.get('mode') or ''}",
        f"- base_branch: {branch.get('base_branch') or ''}",
        f"- work_branch: {branch.get('work_branch') or ''}",
        f"- pr_target: {branch.get('pr_target') or ''}",
        f"- delivery_mode: {delivery_mode}",
        "",
    ]
    if workspace.get("isolated"):
        lines += [
            "Workspace isolation:",
            "- TaskLane prepared an isolated git worktree for this job.",
            f"- original_repo_path: {workspace.get('original_repo_path') or ''}",
            f"- worktree_path: {workspace.get('worktree_path') or repo.get('path') or ''}",
            "- Work only in the repo path above; do not edit the original checkout.",
            "",
        ]
    lines += [
        "Scope contract:",
        f"- allowed_paths: {', '.join(map(str, allowed_paths)) if allowed_paths else '(none declared)'}",
        f"- denied_paths: {', '.join(map(str, denied_paths)) if denied_paths else '(none declared)'}",
        f"- allow_unlisted_paths: {scope.get('allow_unlisted_paths', True)}",
        "",
        "Acceptance criteria:",
        *(f"- {item}" for item in acceptance),
        "",
        "Operational rules:",
        "- Use the repo path above; do not work outside the target repo.",
        "- If branch mode is new-branch, create/switch to work_branch from base_branch.",
        "- If delivery mode is pull-request, commit, push work_branch, and open a PR against pr_target.",
        "- If delivery mode is direct-push, commit and push the target work_branch only.",
        "- If delivery mode is report-only, do not commit or push; return findings only.",
        "- Never modify denied paths. If allow_unlisted_paths is false, modify only allowed_paths.",
        "- End with changed files, verification performed, delivery URL/branch, and residual risks.",
    ]
    return "\n".join(lines)


def _find_pull_request(*, repo_path: Path, work_branch: str, pr_target: str) -> Dict[str, Any]:
    proc = subprocess.run(
        ["gh", "pr", "list", "--head", work_branch, "--base", pr_target, "--state", "open",
         "--json", "url,number,state,title", "--limit", "1"],
        cwd=str(repo_path), capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT,
    )
    if proc.returncode != 0:
        return {"ok": False, "reason": "gh pr list failed; PR was not verified", "stderr": proc.stderr[-2000:]}
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        return {"ok": False, "reason": f"gh pr list returned invalid JSON: {exc}"}
    if not rows:
        return {"ok": False, "reason": "no open pull request found for work_branch/pr_target"}
    return {"ok": True, "pr": dict(rows[0])}


def validate_job_delivery(record: Dict[str, Any], final_response: str) -> Dict[str, Any]:
    spec = dict(record.get("spec") or {})
    delivery_mode = str(spec.get("delivery_mode") or "").strip().lower()
    raw_repo_path = str((spec.get("repo") or {}).get("path") or "").strip()
    repo_path = Path(raw_repo_path).expanduser()
    branch = dict(spec.get("branch") or {})
    work_branch = str(branch.get("work_branch") or "").strip()
    pr_target = str(branch.get("pr_target") or branch.get("base_branch") or "").strip()

    if delivery_mode not in {"pull-request", "direct-push", "report-only"}:
        return {"ok": False, "reason": f"unknown delivery_mode: {delivery_mode or '(empty)'}"}
    if not raw_repo_path or not repo_path.exists():
        return {"ok": False, "reason": f"repo path does not exist: {raw_repo_path or '(empty)'}"}

    status = run_git(["git", "status", "--porcelain"], cwd=repo_path)
    if status.returncode != 0:
        return {"ok": False, "reason": "git status failed", "stderr": status.stderr[-2000:]}
    dirty_lines = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty_lines:
        return {"ok": False, "reason": "worktree has uncommitted changes after agent run", "dirty_files": dirty_lines[:100]}

    if delivery_mode == "report-only":
        return {"ok": True, "delivery_mode": delivery_mode, "worktree_clean": True}

    branch_status = run_git(["git", "status", "--porcelain", "--branch"], cwd=repo_path)
    branch_summary = branch_status.stdout.splitlines()[0] if branch_status.stdout else ""
    if delivery_mode == "direct-push":
        unpushed = run_git(["git", "log", "--oneline", "HEAD", "--not", "--remotes"], cwd=repo_path)
        if unpushed.returncode != 0:
            return {"ok": False, "reason": "direct-push job could not verify pushed commits", "stderr": unpushed.stderr[-2000:]}
        # NOTE: do not use the "[ahead N]" marker from `git status --branch` here —
        # a work branch created from origin/<base> tracks the *base* branch, so it
        # reads "ahead" even when fully pushed to its own remote ref. `git log HEAD
        # --not --remotes` is the authoritative unpushed-commit check.
        if unpushed.stdout.strip():
            return {"ok": False, "reason": "direct-push job has local commits that are not pushed",
                    "branch_status": branch_summary, "unpushed_commits": unpushed.stdout.splitlines()[:20]}
        return {"ok": True, "delivery_mode": delivery_mode, "worktree_clean": True, "branch_status": branch_summary}

    if not work_branch:
        return {"ok": False, "reason": "pull-request delivery requires branch.work_branch"}
    if not pr_target:
        return {"ok": False, "reason": "pull-request delivery requires branch.pr_target"}
    pr = _find_pull_request(repo_path=repo_path, work_branch=work_branch, pr_target=pr_target)
    if not pr.get("ok"):
        return {"ok": False, "reason": pr.get("reason") or "pull request was not verified",
                "delivery_mode": delivery_mode, "work_branch": work_branch, "pr_target": pr_target,
                "branch_status": branch_summary,
                "final_response_has_pr_url": bool(re.search(r"https://github\.com/[^\s)]+/pull/\d+", final_response or "")),
                **{k: v for k, v in pr.items() if k != "ok"}}
    return {"ok": True, "delivery_mode": delivery_mode, "worktree_clean": True,
            "work_branch": work_branch, "pr_target": pr_target, "pr": pr.get("pr")}
