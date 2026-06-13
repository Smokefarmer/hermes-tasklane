"""TaskLane worker — claims ready jobs and runs them via the Claude Code CLI.

Standalone replacement for Hermes's gateway job watcher. Polls the file-backed
job store, claims ready jobs (atomic file locks), prepares an isolated git
worktree, runs ``claude -p``, validates delivery, and marks the job
completed/blocked/failed. Per-job output is captured under
``~/.tasklane/jobs/logs/<job_id>.log``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from tasklane.config import Config, load_config
from tasklane.fanout import create_proposed_drafts
from tasklane.metrics import merge_metrics, run_metrics, spend_last_24h
from tasklane.paths import logs_root
from tasklane.reconcile import backoff_not_before, reconcile
from tasklane.runner import run_claude_cli_job
from tasklane.store import JobStore
from tasklane.worktree import (
    WorktreePreparationError,
    cleanup_worktree,
    job_prompt,
    prepare_job_workspace,
    validate_job_delivery,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("tasklane.worker")

_OWNER = f"worker-{os.getpid()}"


def _repair_attempts() -> int:
    try:
        return max(0, min(3, int(os.getenv("TASKLANE_DELIVERY_REPAIR_ATTEMPTS", "1"))))
    except (TypeError, ValueError):
        return 1


def _job_log(job_id: str) -> Path:
    return logs_root() / f"{job_id}.log"


def _append_log(job_id: str, text: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    with _job_log(job_id).open("a", encoding="utf-8") as handle:
        handle.write(f"\n===== {stamp} =====\n{text}\n")


def _repair_prompt(record: Dict[str, Any], validation: Dict[str, Any], final_response: str, *, attempt: int, max_attempts: int) -> str:
    spec = record.get("spec") if isinstance(record.get("spec"), dict) else {}
    repo = spec.get("repo") if isinstance(spec.get("repo"), dict) else {}
    branch = spec.get("branch") if isinstance(spec.get("branch"), dict) else {}
    delivery_mode = str(spec.get("delivery_mode") or "pull-request").strip().lower()
    validation_json = json.dumps(validation, indent=2, sort_keys=True, ensure_ascii=True)[:6000]
    return "\n".join([
        "TaskLane delivery validation failed. This is a bounded delivery-repair pass.",
        "",
        f"Repair attempt: {attempt}/{max_attempts}",
        f"Job ID: {record.get('id')}",
        f"Repo path: {repo.get('path') or ''}",
        f"Delivery mode: {delivery_mode}",
        f"Work branch: {branch.get('work_branch') or ''}",
        f"PR target: {branch.get('pr_target') or branch.get('base_branch') or ''}",
        "",
        "Validation failure:",
        validation_json,
        "",
        "Previous final response:",
        (final_response or "").strip()[:3000] or "(empty)",
        "",
        "Repair contract:",
        "1. Start by inspecting git status --short --branch in the repo path.",
        "2. Do not broaden scope or start new feature work.",
        "3. If the current changes are correct, finish verification and deliver per the delivery mode.",
        "4. If the changes are unsafe/incomplete, revert only your own job changes and leave the worktree clean.",
        "5. Never stop with uncommitted changes.",
        "",
        "Finish with changed files, verification performed, delivery URL/branch, and residual risks.",
    ])


def _fail_or_requeue(store: JobStore, cfg: Config, job_id: str, *, reason: str,
                     metrics: Dict[str, Any] | None = None) -> str:
    """Auto-retry a transient failure with backoff, or park it in blocked at the attempt cap.

    The record is re-read so the attempt count reflects any transition that
    happened after this worker claimed the job (e.g. a concurrent reconcile).
    """
    record = store.get(job_id)
    if record is None:
        logger.error("Job %s vanished before fail/requeue (reason was: %s)", job_id, reason)
        return "not-found"
    attempt = int(record.get("attempt") or 0)
    if attempt < cfg.max_attempts:
        not_before = backoff_not_before(cfg, attempt)
        store.append_event(job_id, "job_auto_retry_scheduled", state="running", reason=reason,
                           metadata={"attempt": attempt, "max_attempts": cfg.max_attempts, "not_before": not_before})
        store.requeue(job_id, reason="auto-retry-scheduled", not_before=not_before, last_error=reason, metrics=metrics)
        return "requeued"
    store.fail(job_id, reason=f"{reason} (attempt cap reached: {attempt}/{cfg.max_attempts})",
               run_id=job_id, retryable=True, metrics=metrics)
    return "blocked"


_CONTEXT_EXCERPT_CHARS = 4000


def inject_upstream_context(store: JobStore, record: Dict[str, Any]) -> Dict[str, Any]:
    """Attach completed upstream jobs' final responses (spec.context_from) to the
    record so job_prompt renders them. Missing/unfinished upstream jobs are noted
    rather than fatal — dependencies gating should normally prevent that."""
    spec = record.get("spec") if isinstance(record.get("spec"), dict) else {}
    upstream_ids = [str(j).strip() for j in spec.get("context_from") or [] if str(j).strip()]
    if not upstream_ids:
        return record
    entries = []
    for upstream_id in upstream_ids:
        upstream = store.get(upstream_id)
        if upstream is None:
            entries.append({"job_id": upstream_id, "note": "upstream job not found"})
            continue
        response = str(((upstream.get("result") or {}).get("final_response")) or "").strip()
        entries.append({
            "job_id": upstream_id,
            "title": ((upstream.get("spec") or {}).get("request") or {}).get("title"),
            "state": upstream.get("state"),
            "final_response": response[:_CONTEXT_EXCERPT_CHARS] or "(no final response recorded)",
        })
    prepared = dict(record)
    prepared_spec = dict(spec)
    metadata = dict(prepared_spec.get("metadata") or {})
    metadata["upstream_context"] = entries
    prepared_spec["metadata"] = metadata
    prepared["spec"] = prepared_spec
    return prepared


def _fanout_proposals(store: JobStore, completed: Dict[str, Any], final_response: str) -> None:
    """Create draft jobs from a completed job's proposed_tasks block (best-effort).

    Fan-out must never turn a successfully completed job into a failure, so any
    unexpected error is swallowed and recorded as an event rather than raised.
    """
    job_id = str(completed.get("id") or "")
    try:
        create_proposed_drafts(store, completed, final_response)
    except Exception as exc:  # noqa: BLE001 — fan-out is best-effort post-completion
        logger.warning("Job %s fan-out error: %s", job_id, exc)
        store.append_event(job_id, "job_fanout_error", reason=str(exc))


def run_job(store: JobStore, cfg: Config, record: Dict[str, Any]) -> None:
    """Execute one already-claimed (running) job to completion/failure."""
    job_id = str(record.get("id") or "")
    store.append_event(job_id, "job_agent_started", state="running", reason="worker-started")
    worktree_info: Dict[str, Any] | None = None
    started_at = datetime.now(timezone.utc)
    collected_runs: list[Dict[str, Any]] = []

    def _job_metrics() -> Dict[str, Any]:
        # merge this attempt's runs (main + repairs) with prior attempts' totals
        prior = (store.get(job_id) or {}).get("metrics")
        wall = {"wall_seconds": round((datetime.now(timezone.utc) - started_at).total_seconds(), 1)}
        return merge_metrics(prior, *collected_runs, wall)

    try:
        prepared_record, worktree_info = prepare_job_workspace(record)
        if worktree_info:
            store.append_event(job_id, "job_workspace_prepared", state="running", reason="isolated-worktree-ready",
                               metadata={k: worktree_info.get(k) for k in ("original_repo_path", "worktree_path", "mode", "base_ref", "reused")})
            # Persist worktree location on the record so the MCP fix-tools can find
            # the worktree of a blocked/failed job (kept on failure).
            store.transition(job_id, "running", reason="workspace-recorded", updates={"worktree": worktree_info})
        prepared_record = inject_upstream_context(store, prepared_record)
        prompt = job_prompt(prepared_record)
        workspace_path = str((worktree_info or {}).get("worktree_path") or "") or str((prepared_record.get("spec") or {}).get("repo", {}).get("path") or "")
        _append_log(job_id, f"PROMPT:\n{prompt}")

        def _run(prompt_text: str) -> Dict[str, Any]:
            result = run_claude_cli_job(
                prompt_text,
                cwd=workspace_path or os.getcwd(),
                model=cfg.default_model,
                permission_mode=cfg.permission_mode,
                timeout_seconds=cfg.timeout_seconds,
            )
            collected_runs.append(run_metrics(result))
            return result

        result = _run(prompt)
        final_response = (result or {}).get("final_response") or ""
        _append_log(job_id, f"RESULT error={result.get('error')!r}\nFINAL_RESPONSE:\n{final_response}")
        if result.get("error"):
            raise RuntimeError(str(result.get("error")))
        store.append_event(job_id, "job_agent_completed", state="running", reason="agent-loop-completed",
                           metadata={"final_response_preview": final_response[:500]})

        validation = validate_job_delivery(prepared_record, final_response)
        max_repairs = _repair_attempts()
        for attempt in range(1, max_repairs + 1):
            if validation.get("ok"):
                break
            store.append_event(job_id, "job_delivery_repair_started", state="running",
                               reason=str(validation.get("reason") or "delivery validation failed"),
                               metadata={"attempt": attempt, "validation": validation})
            repair_result = _run(_repair_prompt(prepared_record, validation, final_response, attempt=attempt, max_attempts=max_repairs))
            _append_log(job_id, f"REPAIR {attempt} error={repair_result.get('error')!r}\n{(repair_result.get('final_response') or '')}")
            if repair_result.get("error"):
                validation = {"ok": False, "reason": f"delivery repair failed: {repair_result.get('error')}", "previous_validation": validation}
                break
            if repair_result.get("final_response"):
                final_response = repair_result["final_response"]
            validation = validate_job_delivery(prepared_record, final_response)

        if not validation.get("ok"):
            reason = str(validation.get("reason") or "delivery validation failed")
            store.append_event(job_id, "job_delivery_validation_failed", state="running", reason=reason, metadata=validation)
            store.fail(job_id, reason=reason, run_id=job_id, retryable=True, metrics=_job_metrics())  # -> blocked, worktree kept
            cleanup_worktree(worktree_info, keep=True)
            logger.warning("Job %s blocked: %s", job_id, reason)
            return

        cleanup_worktree(worktree_info, keep=False)
        completed = store.complete(job_id, run_id=job_id,
                                   result={"final_response": final_response[:8000], "delivery_validation": validation},
                                   metrics=_job_metrics())
        logger.info("Job %s completed", job_id)
        _fanout_proposals(store, completed, final_response)

    except WorktreePreparationError as exc:
        _append_log(job_id, f"WORKSPACE ERROR: {exc}")
        store.append_event(job_id, "job_workspace_preparation_failed", state="running", reason=str(exc))
        outcome = _fail_or_requeue(store, cfg, job_id, reason=f"workspace preparation failed: {exc}", metrics=_job_metrics())
        cleanup_worktree(worktree_info, keep=True)
        logger.warning("Job %s workspace prep failed (%s): %s", job_id, outcome, exc)
    except Exception as exc:  # noqa: BLE001 — surface any failure to the store
        _append_log(job_id, f"AGENT ERROR: {exc}")
        store.append_event(job_id, "job_agent_failed", state="running", reason=str(exc))
        outcome = _fail_or_requeue(store, cfg, job_id, reason=str(exc), metrics=_job_metrics())
        cleanup_worktree(worktree_info, keep=True)
        logger.warning("Job %s failed (%s): %s", job_id, outcome, exc)


def _repo_key(record: Dict[str, Any]) -> str:
    return str((((record.get("spec") or {}).get("repo")) or {}).get("key") or "").strip()


def _reconcile_safely(store: JobStore, cfg: Config) -> None:
    try:
        report = reconcile(store, cfg)
        if report["orphans"] or report["stale_locks_removed"]:
            logger.info("Reconcile: %s", json.dumps(report, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 — recovery must never kill the loop
        logger.warning("Reconcile error: %s", exc)


def worker_loop(stop: threading.Event) -> None:
    cfg = load_config()
    store = JobStore()
    store.ensure_dirs()
    logger.info("TaskLane worker started (owner=%s, max_in_progress=%s, poll=%ss)", _OWNER, cfg.max_in_progress, cfg.poll_interval_seconds)
    _reconcile_safely(store, cfg)  # recover orphans from a previous worker right away
    last_reconcile = time.monotonic()
    budget_paused = False
    with ThreadPoolExecutor(max_workers=max(1, cfg.max_in_progress)) as pool:
        while not stop.is_set():
            try:
                cfg = load_config()  # pick up config edits live
                if time.monotonic() - last_reconcile >= cfg.reconcile_interval_seconds:
                    _reconcile_safely(store, cfg)
                    last_reconcile = time.monotonic()
                if cfg.daily_budget_usd > 0:
                    spend = spend_last_24h(store)
                    if spend >= cfg.daily_budget_usd:
                        if not budget_paused:
                            logger.warning("Daily budget reached ($%.2f >= $%.2f/24h); pausing new claims",
                                           spend, cfg.daily_budget_usd)
                            budget_paused = True
                        stop.wait(cfg.poll_interval_seconds)
                        continue
                    if budget_paused:
                        logger.info("Spend back under budget ($%.2f < $%.2f/24h); resuming claims",
                                    spend, cfg.daily_budget_usd)
                        budget_paused = False
                running_records = store.list(states=["running"])
                slots = max(0, cfg.max_in_progress - len(running_records))
                # serialize per repo: two jobs on the same repo can collide on
                # branches/remotes even in separate worktrees
                active_repo_keys = {_repo_key(r) for r in running_records if _repo_key(r)} if cfg.serialize_per_repo else set()
                for _ in range(slots):
                    record = store.claim_next(owner=_OWNER, active_repo_keys=active_repo_keys)
                    if not record:
                        break
                    logger.info("Claimed job %s", record.get("id"))
                    if cfg.serialize_per_repo and _repo_key(record):
                        active_repo_keys.add(_repo_key(record))
                    pool.submit(run_job, store, cfg, record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Worker loop error: %s", exc)
            stop.wait(cfg.poll_interval_seconds)
    logger.info("TaskLane worker stopped")


def main() -> None:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    worker_loop(stop)


if __name__ == "__main__":
    main()
