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


def _opt_str(value: Any) -> str | None:
    return (str(value).strip() or None) if value else None


def _normalize_entry(
    *,
    name: Any,
    test_command: Any,
    build_command: Any,
    docs: Any,
    base_branch: Any,
    default_model: Any,
    merge_policy: Any,
    env_file: Any = None,
    local_test_command: Any = None,
    staging_url: Any = None,
    staging_url_command: Any = None,
    test_notes: Any = None,
) -> Dict[str, Any]:
    """Coerce raw fields into a validated, JSON/YAML-safe profile dict.

    The ``env_file`` / ``*_test_command`` / ``staging_*`` / ``test_notes`` fields
    drive TESTER roles (test-local / test-staging). ``env_file`` is the ONLY place
    a project's test secrets live; the worker injects its values into the job
    subprocess and exposes only KEY NAMES to the prompt (see worker / secrets).
    """
    policy = str(merge_policy or "manual").strip().lower()
    if policy not in _MERGE_POLICIES:
        policy = "manual"
    return {
        "name": str(name).strip() if name else "",
        "test_command": _opt_str(test_command),
        "build_command": _opt_str(build_command),
        "docs": [str(d).strip() for d in (docs or []) if str(d).strip()],
        "base_branch": str(base_branch).strip() if base_branch else "main",
        "default_model": _opt_str(default_model),
        "merge_policy": policy,
        # tester fields (all optional)
        "env_file": _opt_str(env_file),
        "local_test_command": _opt_str(local_test_command),
        "staging_url": _opt_str(staging_url),
        "staging_url_command": _opt_str(staging_url_command),
        "test_notes": _opt_str(test_notes),
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
    env_file: str | None = None,
    local_test_command: str | None = None,
    staging_url: str | None = None,
    staging_url_command: str | None = None,
    test_notes: str | None = None,
) -> Dict[str, Any]:
    """Create or update a project profile. ``path`` must be a git repository.

    Returns the stored entry with its canonical registry key under ``"path"``.
    Raises ``ValueError`` if ``path`` is not a git repository. Re-registering a
    project merges over its existing entry, so tester fields set later (e.g. by
    ``set_project_env_file``) survive a plain re-register that omits them.
    """
    top = _git_toplevel(Path(str(path)).expanduser())
    if not top:
        raise ValueError(f"not a git repository: {path}")
    existing = _load_registry().get(top, {})
    entry = _normalize_entry(
        name=name or existing.get("name") or Path(top).name,
        test_command=test_command if test_command is not None else existing.get("test_command"),
        build_command=build_command if build_command is not None else existing.get("build_command"),
        docs=docs if docs is not None else existing.get("docs"),
        base_branch=base_branch if base_branch is not None else existing.get("base_branch") or "main",
        default_model=default_model if default_model is not None else existing.get("default_model"),
        merge_policy=merge_policy if merge_policy is not None else existing.get("merge_policy") or "manual",
        env_file=env_file if env_file is not None else existing.get("env_file"),
        local_test_command=local_test_command if local_test_command is not None else existing.get("local_test_command"),
        staging_url=staging_url if staging_url is not None else existing.get("staging_url"),
        staging_url_command=staging_url_command if staging_url_command is not None else existing.get("staging_url_command"),
        test_notes=test_notes if test_notes is not None else existing.get("test_notes"),
    )
    projects = _load_registry()
    projects[top] = entry
    _write_registry(projects)
    return {"path": top, **entry}


def set_project_env_file(path: str, env_file: str) -> Dict[str, Any]:
    """Point a registered project's ``env_file`` at ``env_file`` (used by the
    set_project_secrets intake tool). The project must already be registered.
    Returns the updated entry with its canonical key under ``"path"``."""
    top = _git_toplevel(Path(str(path)).expanduser())
    if not top:
        raise ValueError(f"not a git repository: {path}")
    projects = _load_registry()
    if top not in projects:
        raise ValueError(f"project not registered: {top} (call register_project first)")
    projects[top] = {**projects[top], "env_file": str(env_file).strip() or None}
    _write_registry(projects)
    return {"path": top, **projects[top]}


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
