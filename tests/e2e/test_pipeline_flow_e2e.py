"""E2E: a full plan -> implement -> review assembly line through the real worker
loop — dependency gating, upstream-context passing, and delivery all verified."""

import json
import subprocess
import threading

import pytest

from tasklane.mcp_server import _pipeline_stage_specs
from tasklane.store import JobStore
from tasklane.worker import worker_loop

from .test_worker_loop_e2e import wait_for_state

pytestmark = pytest.mark.e2e


def test_pipeline_plan_implement_review(tasklane_home, fake_claude, git_repo, monkeypatch):
    specs = _pipeline_stage_specs(
        "pipe", str(git_repo), "Pipeline feature", "build the pipeline feature",
        stages=["plan", "implement", "review"],
        work_branch="tasklane/pipe", base_branch="main",
        pr_target=None, delivery_mode="direct-push",
    )
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO_MAP", json.dumps({
        "pipe-plan": "report_only",
        "pipe-implement": "commit_and_push",
        "pipe-review": "report_only",
    }))

    store = JobStore()
    store.ensure_dirs()
    for spec in specs:
        store.put(spec)

    stop = threading.Event()
    thread = threading.Thread(target=worker_loop, args=(stop,), daemon=True)
    thread.start()
    try:
        plan = wait_for_state(store, "pipe-plan", {"completed", "blocked", "failed"})
        implement = wait_for_state(store, "pipe-implement", {"completed", "blocked", "failed"})
        review = wait_for_state(store, "pipe-review", {"completed", "blocked", "failed"})
    finally:
        stop.set()
        thread.join(timeout=10)

    assert plan["state"] == "completed", plan.get("last_error")
    assert implement["state"] == "completed", implement.get("last_error")
    assert review["state"] == "completed", review.get("last_error")

    # context passing: implement saw the plan's output; review saw implement's
    implement_prompt = (fake_claude / "prompt-pipe-implement.txt").read_text()
    assert "upstream job pipe-plan" in implement_prompt
    assert "Analysis complete" in implement_prompt
    review_prompt = (fake_claude / "prompt-pipe-review.txt").read_text()
    assert "upstream job pipe-implement" in review_prompt
    assert "Committed and pushed" in review_prompt

    # the implement stage really delivered to origin
    origin = git_repo.parent / "origin.git"
    log = subprocess.run(["git", "log", "--oneline", "tasklane/pipe"],
                         cwd=origin, capture_output=True, text=True)
    assert log.returncode == 0 and "feat: fake change" in log.stdout

    # dependency gating: stages ran strictly in order
    assert plan["completed_at"] <= implement["completed_at"] <= review["completed_at"]
