#!/usr/bin/env python3
"""Fake ``claude`` CLI for E2E tests.

Installed on PATH as ``claude`` by the e2e conftest. Mimics the real CLI's
contract used by tasklane.runner: invoked as ``claude -p <prompt>
--output-format json ...`` with cwd = the job worktree, and prints a JSON
object ``{"result": str, "is_error": bool}``.

Behavior is selected via the FAKE_CLAUDE_SCENARIO environment variable; each
invocation increments a call counter under FAKE_CLAUDE_STATE so tests can
assert on repair-pass behavior.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def arg(flag: str) -> str:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else ""


def out(result: str, is_error: bool = False) -> None:
    print(json.dumps({
        "result": result,
        "is_error": is_error,
        # telemetry fields mirroring the real CLI's JSON output
        "total_cost_usd": 0.05,
        "duration_ms": 1200,
        "num_turns": 3,
        "session_id": "fake-session",
        "usage": {"input_tokens": 1000, "output_tokens": 250,
                  "cache_read_input_tokens": 400, "cache_creation_input_tokens": 100},
    }))
    sys.exit(0)


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


def main() -> None:
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "report_only")
    prompt = arg("-p")
    is_repair = prompt.startswith("TaskLane delivery validation failed")

    state_dir = os.environ.get("FAKE_CLAUDE_STATE")
    if state_dir:
        calls = Path(state_dir) / "calls"
        n = int(calls.read_text()) if calls.exists() else 0
        calls.write_text(str(n + 1))

    if scenario == "report_only":
        out("Analysis complete. No changes required.")
    elif scenario == "error":
        out("simulated agent failure", is_error=True)
    elif scenario == "commit_and_push":
        Path("FEATURE.txt").write_text("made by fake claude\n")
        git("add", "FEATURE.txt")
        git("commit", "-m", "feat: fake change")
        git("push", "origin", "HEAD")
        out("Committed and pushed FEATURE.txt.")
    elif scenario == "dirty":
        Path("JUNK.txt").write_text("uncommitted\n")
        out("Done (left an uncommitted file behind).")
    elif scenario == "dirty_then_clean":
        junk = Path("JUNK.txt")
        if is_repair:
            junk.unlink(missing_ok=True)
            out("Repair pass: removed the uncommitted file.")
        junk.write_text("uncommitted\n")
        out("Done (left an uncommitted file behind).")
    else:
        out(f"unknown FAKE_CLAUDE_SCENARIO: {scenario}", is_error=True)


if __name__ == "__main__":
    main()
