"""Render role-based stage prompt templates against a job record.

Builds the operational context blocks (branch/workspace, scope, delivery rules,
upstream context) that the generic prompt already produces, then fills a role
template's ``{placeholder}`` fields with them. Kept separate from
``tasklane.worktree`` so the generic-prompt module stays small and focused.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tasklane.prompts.render")

#: Placeholder fields a role template may reference.
PLACEHOLDERS = (
    "job_id", "title", "body", "repo_path",
    "branch_contract", "upstream_context", "scope_contract", "delivery_contract",
    "env_vars", "test_context",
)


def _branch_contract_block(branch: Dict[str, Any], delivery_mode: str,
                           workspace: Dict[str, Any], repo: Dict[str, Any]) -> str:
    """Branch contract plus the workspace-isolation note (kept for role prompts)."""
    lines = [
        "Branch contract:",
        f"- mode: {branch.get('mode') or ''}",
        f"- base_branch: {branch.get('base_branch') or ''}",
        f"- work_branch: {branch.get('work_branch') or ''}",
        f"- pr_target: {branch.get('pr_target') or ''}",
        f"- delivery_mode: {delivery_mode}",
    ]
    if workspace.get("isolated"):
        lines += [
            "",
            "Workspace isolation:",
            "- TaskLane prepared an isolated git worktree for this job.",
            f"- original_repo_path: {workspace.get('original_repo_path') or ''}",
            f"- worktree_path: {workspace.get('worktree_path') or repo.get('path') or ''}",
            "- Work only in the repo path above; do not edit the original checkout.",
        ]
    return "\n".join(lines)


def _upstream_context_block(spec: Dict[str, Any]) -> str:
    """Render upstream pipeline context, or '' when there is none."""
    upstream = (spec.get("metadata") or {}).get("upstream_context") or []
    lines: List[str] = []
    for entry in upstream:
        if not isinstance(entry, dict):
            continue
        if not lines:
            lines += ["Context from upstream pipeline jobs (read before planning):", ""]
        header = f"--- upstream job {entry.get('job_id')}"
        if entry.get("title"):
            header += f" ({entry['title']})"
        header += f" [{entry.get('state') or '?'}] ---"
        lines += [header, str(entry.get("final_response") or entry.get("note") or ""), ""]
    return "\n".join(lines).rstrip("\n")


def _env_vars_block(spec: Dict[str, Any]) -> str:
    """List the env-var KEY NAMES available to the run — never the values.

    The worker injects the secret values straight into the agent subprocess env;
    only the sorted key names are surfaced here so a tester knows what credentials
    it can use without any value ever touching a prompt or log.
    """
    names = (spec.get("metadata") or {}).get("env_var_names") or []
    clean = sorted({str(name).strip() for name in names if str(name).strip()})
    if not clean:
        return (
            "Environment variables: none injected for this job. Obtain any config "
            "you need via the documented local_test_command / railway CLI (do not "
            "invent credentials)."
        )
    return (
        "Environment variables available (NAMES ONLY — the values are already in "
        "your runtime environment; read them with os.environ / $VAR, and NEVER "
        f"print or echo a value): {', '.join(clean)}"
    )


def _test_context_block(spec: Dict[str, Any]) -> str:
    """Render the project's non-secret test/deploy context, or '' when absent."""
    context = (spec.get("metadata") or {}).get("test_context") or {}
    if not isinstance(context, dict) or not context:
        return ""
    labels = (
        ("local_test_command", "Local test/boot command"),
        ("staging_url", "Staging URL"),
        ("staging_url_command", "Staging URL command (run to discover the URL)"),
        ("test_notes", "Project test notes"),
    )
    lines: List[str] = ["Project test context:"]
    for key, label in labels:
        value = context.get(key)
        if value:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _scope_contract_block(scope: Dict[str, Any]) -> str:
    allowed_paths = scope.get("allowed_paths") or []
    denied_paths = scope.get("denied_paths") or []
    return "\n".join([
        "Scope contract:",
        f"- allowed_paths: {', '.join(map(str, allowed_paths)) if allowed_paths else '(none declared)'}",
        f"- denied_paths: {', '.join(map(str, denied_paths)) if denied_paths else '(none declared)'}",
        f"- allow_unlisted_paths: {scope.get('allow_unlisted_paths', True)}",
    ])


def _delivery_contract_block(request: Dict[str, Any]) -> str:
    """Acceptance criteria plus the operational delivery rules."""
    acceptance = request.get("acceptance_criteria") or []
    lines: List[str] = []
    if acceptance:
        lines += ["Acceptance criteria:", *(f"- {item}" for item in acceptance), ""]
    lines += [
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


def render_role_prompt(template: str, record: Dict[str, Any], spec: Dict[str, Any]) -> Optional[str]:
    """Fill a role template's ``{placeholder}`` fields from the job record.

    Returns ``None`` when the template is malformed (unknown placeholder or stray
    brace), so the caller can fall back to the generic prompt instead of crashing.
    """
    request = spec.get("request") if isinstance(spec.get("request"), dict) else {}
    repo = spec.get("repo") if isinstance(spec.get("repo"), dict) else {}
    branch = spec.get("branch") if isinstance(spec.get("branch"), dict) else {}
    scope = spec.get("scope") if isinstance(spec.get("scope"), dict) else {}
    delivery_mode = spec.get("delivery_mode") or "pull-request"
    workspace = (spec.get("metadata") or {}).get("workspace") or {}
    fields = {
        "job_id": record.get("id") or "",
        "title": request.get("title") or "",
        "body": request.get("body") or request.get("title") or "",
        "repo_path": repo.get("path") or "",
        "branch_contract": _branch_contract_block(branch, delivery_mode, workspace, repo),
        "upstream_context": _upstream_context_block(spec),
        "scope_contract": _scope_contract_block(scope),
        "delivery_contract": _delivery_contract_block(request),
        "env_vars": _env_vars_block(spec),
        "test_context": _test_context_block(spec),
    }
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("Failed to render role prompt template (%s); using generic prompt", exc)
        return None
