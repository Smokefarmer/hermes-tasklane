# Recipe: overnight batch

Queue a stack of independent, well-scoped jobs before you log off and review the
results in the morning. TaskLane runs them in the background on your Claude
subscription, auto-retrying transient failures, and parks anything that needs you
in `blocked`.

## When to use

- You have a backlog of small-to-medium, independent tasks (test backfill,
  dependency bumps, lint cleanups, a queue of bugfixes across several repos).
- Each task has clear acceptance criteria and a verify command.
- You're happy to review and merge in the morning, not interactively tonight.

If tasks depend on each other, use a **pipeline** (see `feature-pipeline.md`) or
a stacked review (see `pr-review-bot.md`) instead.

## Before you start

Check headroom so the batch doesn't stall halfway through the night:

```
metrics()          # store-wide + 24h spend; confirm you're under daily_budget_usd
worker_status()    # worker active? queue depth by state?
```

If you want several jobs running at once across different repos, make sure the
worker's `max_in_progress` is raised; `serialize_per_repo` keeps same-repo jobs
from clobbering each other, so cross-repo parallelism is safe by default.

## Queue the batch

Submit each job with a full brief (goal, acceptance criteria, verify command,
non-goals, files in scope). One `create_task` per task:

```
create_task(
  repo="/home/me/dev/api",
  title="Backfill tests for auth/token.py",
  body="""
GOAL: bring auth/token.py to >90% line coverage.
ACCEPTANCE CRITERIA: cover expiry, refresh, and tamper paths; no behavior change.
VERIFY: `pytest tests/test_token.py -q --cov=auth/token` shows >90%.
NON-GOALS: do not change token.py's public API.
FILES IN SCOPE: tests/test_token.py (new), read auth/token.py.
""",
  base_branch="develop", work_branch="test/token-coverage",
  delivery_mode="pull-request", pr_target="develop",
)

create_task(repo="/home/me/dev/web", title="Bump eslint to v9", body="…")
create_task(repo="/home/me/dev/api", title="Fix flaky retry test", body="…")
# …one per task
```

Record the returned ids, then **stop** — do not poll. The worker claims `ready`
jobs every few seconds.

## What to expect overnight

- Jobs move `ready → running → {completed | blocked | failed}`.
- Transient failures (workspace prep, agent hiccups) auto-retry with backoff up
  to the attempt cap before parking in `blocked`.
- Delivery-validation failures go straight to `blocked` with the worktree kept.

## Morning review

```
list_tasks()                      # one-line overview of every job
list_tasks(state="completed")     # ready to review / merge
list_tasks(state="blocked")       # need your help
metrics()                         # what the batch cost
```

For each completed job: open its PR (or `get_diff(job_id)` for direct-push) and
review before merging. For each blocked job, follow the repair flow in
`fire-and-forget-bugfix.md` (§ "If it blocks"): diagnose with `get_diff` /
`task_logs`, fix in the worktree, then `retry_task` with `extra_instructions`.

Once everything is merged or resolved, reclaim disk with `prune_worktrees()`.
