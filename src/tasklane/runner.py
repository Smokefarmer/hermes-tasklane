"""Run Hermes coding jobs through the Claude Code CLI (``claude -p``).

This is the alternative to the in-process ``AIAgent`` job runner. It shells out
to the locally-installed ``claude`` binary, which authenticates with the user's
Claude subscription (OAuth stored in ``~/.claude/.credentials.json``) — no
Anthropic API key required.

Selected per deployment via ``kanban.coding_runner: claude_cli`` in config.yaml
(or the ``HERMES_JOB_RUNNER=claude_cli`` environment override). The default
remains the in-process runner, so both options stay available.

The return shape — ``{"final_response": str, "error": Optional[str]}`` — matches
what the in-process runner produces, so the gateway's delivery-validation and
repair loop consumes it unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import List, Optional

# An inherited Anthropic key would make the CLI bill against API credits (which
# the subscription user does not have) instead of the Claude subscription OAuth.
_STRIP_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_VALID_PERMISSION_MODES = ("acceptEdits", "bypassPermissions", "plan", "default")
_DEFAULT_PERMISSION_MODE = "acceptEdits"
_MIN_TIMEOUT_SECONDS = 60


def claude_cli_available() -> Optional[str]:
    """Return the absolute path to the ``claude`` binary, or ``None``."""
    return shutil.which("claude")


def _build_env() -> dict:
    env = dict(os.environ)
    for var in _STRIP_ENV_VARS:
        env.pop(var, None)
    if not env.get("HOME"):
        env["HOME"] = os.path.expanduser("~")
    return env


def run_claude_cli_job(
    prompt: str,
    *,
    cwd: str,
    model: Optional[str] = None,
    permission_mode: str = _DEFAULT_PERMISSION_MODE,
    timeout_seconds: int = 3600,
    extra_dirs: Optional[List[str]] = None,
) -> dict:
    """Execute one coding job via ``claude -p`` and return its result.

    Args:
        prompt: The full job instruction handed to Claude Code.
        cwd: Working directory for the run (the isolated job worktree).
        model: Optional model alias/id; ``None`` uses the CLI default.
        permission_mode: One of ``acceptEdits`` (default), ``bypassPermissions``,
            ``plan``, ``default``. Invalid values fall back to ``acceptEdits``.
        timeout_seconds: Hard wall-clock cap; floored at 60s.
        extra_dirs: Additional directories the run may access.

    Returns:
        ``{"final_response": str, "error": Optional[str]}``.
    """
    binary = claude_cli_available()
    if not binary:
        return {"final_response": "", "error": "claude CLI not found on PATH"}

    work_dir = cwd or os.getcwd()
    if not os.path.isdir(work_dir):
        return {
            "final_response": "",
            "error": f"job working directory does not exist: {work_dir}",
        }

    if permission_mode not in _VALID_PERMISSION_MODES:
        permission_mode = _DEFAULT_PERMISSION_MODE

    cmd = [
        binary,
        "-p", prompt,
        "--output-format", "json",
        "--permission-mode", permission_mode,
        "--add-dir", work_dir,
    ]
    if model:
        cmd += ["--model", str(model)]
    for extra in extra_dirs or []:
        if extra:
            cmd += ["--add-dir", str(extra)]

    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            env=_build_env(),
            capture_output=True,
            text=True,
            timeout=max(_MIN_TIMEOUT_SECONDS, int(timeout_seconds)),
        )
    except subprocess.TimeoutExpired:
        return {
            "final_response": "",
            "error": f"claude CLI job timed out after {timeout_seconds}s",
        }
    except Exception as exc:  # noqa: BLE001 - surface any spawn failure to the job store
        return {"final_response": "", "error": f"claude CLI job failed to start: {exc}"}

    return _parse_result(proc.stdout, proc.stderr, proc.returncode)


def _extract_metrics(payload: dict) -> dict:
    """Pull usage/cost telemetry out of the CLI's JSON result (fields optional)."""
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    candidates = {
        "cost_usd": payload.get("total_cost_usd"),
        "duration_ms": payload.get("duration_ms"),
        "num_turns": payload.get("num_turns"),
        "session_id": payload.get("session_id"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
    }
    return {key: value for key, value in candidates.items() if value is not None}


def _parse_result(stdout: str, stderr: str, returncode: int) -> dict:
    text = (stdout or "").strip()
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            result_text = str(payload.get("result") or "")
            metrics = _extract_metrics(payload)
            if payload.get("is_error"):
                reason = (
                    result_text
                    or payload.get("api_error_status")
                    or "claude CLI reported an error"
                )
                return {"final_response": result_text, "error": str(reason), "metrics": metrics}
            return {"final_response": result_text, "error": None, "metrics": metrics}

    if returncode != 0:
        err = (stderr or stdout or "").strip()[:2000] or f"claude CLI exited with code {returncode}"
        return {"final_response": "", "error": err, "metrics": {}}
    return {"final_response": text, "error": None, "metrics": {}}
