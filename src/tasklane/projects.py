"""Per-project profiles — a registry of each repo's rules, commands, and docs.

A job's prompt is generic, but every project has its own test/build commands and
authoritative rule files (``CLAUDE.md``, ADRs, ``rules/`` dirs). This module keeps
a file-backed registry at ``$TASKLANE_HOME/projects.yaml`` keyed by the repo's git
toplevel path, and renders a "Project profile:" prompt section so every job that
targets a registered repo is told its commands and which docs it MUST read.

The registry is intentionally self-contained (no import of ``worktree``) so that
``worktree.job_prompt`` and ``worker.run_job`` can import from here without a cycle.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import yaml

from tasklane.paths import tasklane_home

_GIT_TIMEOUT = 30
_MERGE_POLICIES = ("manual", "auto")


def projects_path() -> Path:
    return tasklane_home() / "projects.yaml"


def _git_toplevel(path: Path) -> str | None:
    """Resolved git toplevel of ``path``, or None if it is not a git repo."""
    if not path.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path), capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return str(Path(proc.stdout.strip()).resolve())


def _canonical_key(path: str) -> str:
    """Normalize a repo path to its registry key (git toplevel when available)."""
    expanded = Path(str(path)).expanduser()
    top = _git_toplevel(expanded)
    if top:
        return top
    return str(expanded.resolve())


def _normalize_entry(
    *,
    name: Any,
    test_command: Any,
    build_command: Any,
    docs: Any,
    base_branch: Any,
    default_model: Any,
    merge_policy: Any,
) -> Dict[str, Any]:
    """Coerce raw fields into a validated, JSON/YAML-safe profile dict."""
    policy = str(merge_policy or "manual").strip().lower()
    if policy not in _MERGE_POLICIES:
        policy = "manual"
    return {
        "name": str(name).strip() if name else "",
        "test_command": (str(test_command).strip() or None) if test_command else None,
        "build_command": (str(build_command).strip() or None) if build_command else None,
        "docs": [str(d).strip() for d in (docs or []) if str(d).strip()],
        "base_branch": str(base_branch).strip() if base_branch else "main",
        "default_model": (str(default_model).strip() or None) if default_model else None,
        "merge_policy": policy,
    }


def _load_registry() -> Dict[str, Dict[str, Any]]:
    path = projects_path()
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    projects = raw.get("projects") or {}
    if not isinstance(projects, dict):
        raise ValueError(f"{path}: 'projects' must be a mapping")
    out: Dict[str, Dict[str, Any]] = {}
    for key, entry in projects.items():
        if isinstance(entry, dict):
            out[str(key)] = entry
    return out


def _write_registry(projects: Dict[str, Dict[str, Any]]) -> None:
    path = projects_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump({"projects": projects}, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, path)


def register_project(
    path: str,
    *,
    name: str | None = None,
    test_command: str | None = None,
    build_command: str | None = None,
    docs: List[str] | None = None,
    base_branch: str = "main",
    default_model: str | None = None,
    merge_policy: str = "manual",
) -> Dict[str, Any]:
    """Create or update a project profile. ``path`` must be a git repository.

    Returns the stored entry with its canonical registry key under ``"path"``.
    Raises ``ValueError`` if ``path`` is not a git repository.
    """
    top = _git_toplevel(Path(str(path)).expanduser())
    if not top:
        raise ValueError(f"not a git repository: {path}")
    entry = _normalize_entry(
        name=name or Path(top).name,
        test_command=test_command,
        build_command=build_command,
        docs=docs,
        base_branch=base_branch,
        default_model=default_model,
        merge_policy=merge_policy,
    )
    projects = _load_registry()
    projects[top] = entry
    _write_registry(projects)
    return {"path": top, **entry}


def get_project(path: str) -> Dict[str, Any] | None:
    """Return the profile registered for ``path``'s repo, or None if absent."""
    key = _canonical_key(path)
    return _load_registry().get(key)


def list_projects() -> Dict[str, Dict[str, Any]]:
    """Return the full registry as ``{repo_path: profile}``."""
    return _load_registry()


def render_profile_block(profile: Dict[str, Any] | None, repo_path: str) -> str:
    """Render the "Project profile:" prompt section for a job in ``repo_path``.

    Only docs that actually EXIST in the (worktree) repo are listed as mandatory
    reading, so a job is never told to read a file that is not present.
    Returns "" when there is no profile.
    """
    if not profile:
        return ""
    root = Path(str(repo_path)).expanduser()
    lines = ["Project profile:"]
    if profile.get("name"):
        lines.append(f"- Project: {profile['name']}")
    test_command = profile.get("test_command")
    if test_command:
        lines.append(f"- Test command (verify with exactly this command): {test_command}")
    build_command = profile.get("build_command")
    if build_command:
        lines.append(f"- Build command (verify with exactly this command): {build_command}")
    for doc in profile.get("docs") or []:
        if (root / doc).exists():
            lines.append(
                f"- MANDATORY: read {doc} before planning/implementing; "
                "the reviewer will check your diff against it."
            )
    return "\n".join(lines)


def inject_project_profile(record: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the registered profile (if any) to ``spec.metadata.project_profile``.

    Looks up the ORIGINAL repo path (``spec.metadata.workspace.original_repo_path``
    when worktree isolation ran, else ``spec.repo.path``). Returns a new record;
    the input is left untouched. A missing profile is a no-op.
    """
    spec = record.get("spec") if isinstance(record.get("spec"), dict) else {}
    metadata = spec.get("metadata") if isinstance(spec.get("metadata"), dict) else {}
    workspace = metadata.get("workspace") if isinstance(metadata.get("workspace"), dict) else {}
    original = str(workspace.get("original_repo_path") or "").strip()
    if not original:
        original = str((spec.get("repo") or {}).get("path") or "").strip()
    if not original:
        return record
    profile = get_project(original)
    if not profile:
        return record
    prepared = dict(record)
    prepared_spec = dict(spec)
    prepared_metadata = dict(prepared_spec.get("metadata") or {})
    prepared_metadata["project_profile"] = profile
    prepared_spec["metadata"] = prepared_metadata
    prepared["spec"] = prepared_spec
    return prepared
